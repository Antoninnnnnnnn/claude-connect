import base64
import binascii
import logging
import mimetypes
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings, get_settings
from app.gh_client import GitHubClient, GitHubError, quote_path


logger = logging.getLogger(__name__)
settings = get_settings()
github = GitHubClient(settings)

# GitHub owner and repo names; anything else never reaches upstream.
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
GIT_SERVICES = {"git-upload-pack", "git-receive-pack"}
# Request headers git needs relayed. Git-Protocol carries the protocol v2 request.
GIT_REQUEST_HEADERS = ("content-type", "content-encoding", "accept", "user-agent", "git-protocol")
GIT_RESPONSE_HEADERS = ("content-type", "content-encoding", "expires", "pragma")
AUTH_CHALLENGE = {"WWW-Authenticate": 'Basic realm="claude-connect-github", charset="UTF-8"'}
# Raw files keep their own type only when it is one of these inert binary types; anything
# else (HTML, SVG, XML, `.rs` guessed as application/rls-services+xml, unknown...) is
# served as plain text so nothing renders or runs on this domain. An allowlist, because
# mimetypes also reads the host's /etc/mime.types and guesses differ between machines.
BINARY_TYPES = {
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp", "image/tiff",
    "image/x-icon", "image/vnd.microsoft.icon",
    "application/pdf", "application/zip", "application/gzip", "application/x-tar", "application/x-bzip2",
    "application/x-xz", "application/x-7z-compressed", "application/wasm", "application/octet-stream",
}
BINARY_PREFIXES = ("font/", "audio/", "video/")
# Commit patches are capped so one call stays a readable size; `.diff` has the full one.
MAX_PATCH_CHARS = 20_000
MAX_COMMIT_PATCH_CHARS = 200_000


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.api_key:
        raise RuntimeError("API_KEY is not configured")
    yield
    await github.aclose()


app = FastAPI(
    title="Self-hosted GitHub Proxy",
    version="1.0.0",
    docs_url="/docs",
    lifespan=lifespan,
)


class NoSharedCache:
    """Cloudflare caches .js/.css/.png/... URLs by default when the origin says nothing:
    a private file read once would then be served to anyone, key or not.

    Plain ASGI rather than @app.middleware: that one re-pipes every streamed body, and
    the git relay streams whole packs through here."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "private, no-store"
            await send(message)

        await self.app(scope, receive, send_with_header)


app.add_middleware(NoSharedCache)


class AuthError(Exception):
    pass


class BadRequest(Exception):
    pass


def error_response(status: int, message: str, code: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": message, "error_code": code}, headers=headers)


@app.exception_handler(GitHubError)
async def github_error_handler(_: Request, exc: GitHubError) -> JSONResponse:
    return error_response(exc.status, str(exc), exc.code)


@app.exception_handler(AuthError)
async def auth_error_handler(_: Request, exc: AuthError) -> JSONResponse:
    # The challenge matters: git only sends the user:key from the URL after a 401 asking
    # for Basic, and a browser shows its login prompt on it.
    return error_response(401, str(exc), "unauthorized", AUTH_CHALLENGE)


@app.exception_handler(BadRequest)
async def bad_request_handler(_: Request, exc: BadRequest) -> JSONResponse:
    return error_response(422, str(exc), "invalid_request")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return error_response(422, str(exc.errors()), "invalid_request")


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 404:
        # Mostly github.com URLs this proxy does not mirror (/pull/3, /issues, /compare...).
        return error_response(
            404,
            "Unknown endpoint. Supported: /repos, /<owner>/<repo>, .../tree, .../blob, .../raw, "
            ".../commits, .../commit/<sha>, .../branches, .../archive, and git clone/push",
            "unknown_endpoint",
        )
    return error_response(exc.status_code, str(exc.detail), "invalid_request", getattr(exc, "headers", None))


@app.exception_handler(Exception)
async def generic_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error: %s", exc)
    return error_response(500, "Internal server error", "internal_error")


def presented_keys(request: Request) -> list[str]:
    keys = []
    header = request.headers.get("x-api-key")
    if header:
        keys.append(header)
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "basic" and value:
        try:
            user, _, password = base64.b64decode(value.strip()).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return keys
        # git clone https://x:KEY@host/... puts the key in the password; accept it as
        # the user name too, for KEY@host URLs.
        keys.extend(part for part in (password, user) if part)
    elif scheme.lower() == "bearer" and value:
        keys.append(value.strip())
    return keys


def require_api_key(request: Request, current_settings: Settings = Depends(get_settings)) -> None:
    if not current_settings.api_key:
        raise GitHubError("API_KEY is not configured", status=500, code="misconfigured")
    expected = current_settings.api_key.encode()
    if not any(secrets.compare_digest(key.encode(), expected) for key in presented_keys(request)):
        raise AuthError("Invalid API key")


def repo_name(owner: str, repo: str) -> tuple[str, str]:
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not NAME_RE.match(owner) or not NAME_RE.match(repo) or owner.startswith(".") or repo in {".", ".."}:
        raise BadRequest("Invalid owner or repository name")
    return owner, repo


def check_ref(ref: str) -> str:
    """Branch, tag, SHA or rev expression (HEAD~2). Git ref names can hold neither '..'
    nor ':', and refs end up in API URL paths: '..' there would climb out of the repo."""
    if ".." in ref or ":" in ref or "\\" in ref or len(ref) > 255 or any(ord(c) < 0x20 or c == "\x7f" for c in ref):
        raise BadRequest("Invalid ref")
    return ref


def split_ref_path(rest: str, ref: str | None) -> tuple[str | None, str]:
    """`main/src/app.py` -> ("main", "src/app.py"). A branch with a slash in its name
    is ambiguous in that form: the caller passes it in `ref` and `rest` is the path."""
    return ref_candidates(rest, ref)[0]


def ref_candidates(rest: str, ref: str | None, *, need_path: bool = False) -> list[tuple[str | None, str]]:
    """Ways to read `rest` as <ref>/<path>, most likely first. github.com links put a
    branch like `feature/x` straight in the path (`/blob/feature/x/src/a.py`); when the
    first reading is not found, the next ones take one more segment into the ref."""
    rest = rest.strip("/")
    if ".." in rest.split("/"):
        raise BadRequest("Path may not contain '..'")
    if ref:
        return [(check_ref(ref), rest)]
    parts = rest.split("/") if rest else []
    if not parts:
        return [(None, "")]
    last = len(parts) - 1 if need_path else len(parts)
    first = (check_ref(parts[0]), "/".join(parts[1:]))
    longer = [("/".join(parts[:i]), "/".join(parts[i:])) for i in range(2, min(last, 4) + 1)]
    # A longer reading that cannot be a ref name (a file called a..b) is just skipped.
    return [first] + [(ref, path) for ref, path in longer if is_ref(ref)]


def is_ref(ref: str) -> bool:
    try:
        check_ref(ref)
    except BadRequest:
        return False
    return True


async def first_found(candidates: list[tuple[str | None, str]], fetch):
    """Run fetch(ref, path) on each reading until one is not a 404."""
    first_error = None
    for ref, path in candidates:
        try:
            return ref, path, await fetch(ref, path)
        except GitHubError as exc:
            if exc.code != "not_found":
                raise
            first_error = first_error or exc
    raise first_error


def public_base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def compact_repo(raw: dict[str, Any], base: str) -> dict[str, Any]:
    return {
        "full_name": raw.get("full_name"),
        "private": raw.get("private"),
        "description": raw.get("description"),
        "default_branch": raw.get("default_branch"),
        "language": raw.get("language"),
        "fork": raw.get("fork"),
        "archived": raw.get("archived"),
        "size_kb": raw.get("size"),
        "pushed_at": raw.get("pushed_at"),
        "clone_url": f"{base}/{raw.get('full_name')}.git",
        "can_push": (raw.get("permissions") or {}).get("push"),
    }


def raw_media_type(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    if guessed and guessed != "video/mp2t" and (guessed in BINARY_TYPES or guessed.startswith(BINARY_PREFIXES)):
        return guessed
    return "text/plain; charset=utf-8"


async def body_of(upstream: httpx.Response, *, raw: bool = False) -> AsyncIterator[bytes]:
    """Stream the upstream body and always release its connection, even when the client
    hangs up mid-transfer (an aborted clone must not hold a pooled connection)."""
    try:
        async for chunk in upstream.aiter_raw() if raw else upstream.aiter_bytes():
            yield chunk
    finally:
        await upstream.aclose()


def stream_back(upstream, *, media_type: str, extra: dict[str, str] | None = None) -> StreamingResponse:
    # The CSP sandbox is a second lock behind text/plain + nosniff: even a file a browser
    # decided to render could not run script or reach anything from this origin.
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        **(extra or {}),
    }
    length = upstream.headers.get("content-length")
    if length and not upstream.headers.get("content-encoding"):
        headers["Content-Length"] = length
    return StreamingResponse(
        body_of(upstream),
        status_code=upstream.status_code,
        media_type=media_type,
        headers=headers,
        background=BackgroundTask(upstream.aclose),
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "data": {"status": "up", "token_configured": bool(settings.github_token.strip())}}


@app.get("/repos", dependencies=[Depends(require_api_key)])
async def list_repos(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    page: int = Query(default=1, ge=1),
    sort: str = Query(default="pushed", pattern="^(created|updated|pushed|full_name)$"),
) -> dict[str, Any]:
    if not settings.github_token.strip():
        raise GitHubError("GITHUB_TOKEN is not configured", status=503, code="no_token")
    raw = await github.api_json("/user/repos", {"per_page": limit, "page": page, "sort": sort})
    base = public_base(request)
    repos = [compact_repo(item, base) for item in raw]
    return {"ok": True, "data": {"page": page, "count": len(repos), "has_more": len(repos) == limit, "repos": repos}}


# --- git smart HTTP relay ------------------------------------------------------
# Registered before the browsing routes: `/{owner}/{repo}/info/refs` must never be read
# as a ref named "info".


def git_refusal(status: int, owner: str, repo: str, writing: bool) -> tuple[int, str]:
    has_token = bool(settings.github_token.strip())
    if writing and not has_token:
        return 403, "Pushing needs GITHUB_TOKEN, which is not configured\n"
    if status == 404 or (status == 401 and not has_token):
        # Anonymous, GitHub answers 401 for a private repo as for a missing one.
        hint = "" if has_token else " (GITHUB_TOKEN is not configured: private repos are invisible)"
        return 404, f"Repository {owner}/{repo} not found, or no access to it{hint}\n"
    if status == 401:
        return 403, "GitHub rejected GITHUB_TOKEN (expired or revoked)\n"
    action = "push to" if writing else "read"
    return 403, f"GITHUB_TOKEN is not allowed to {action} {owner}/{repo}\n"


async def relay_git(request: Request, owner: str, repo: str, suffix: str) -> Response:
    owner, repo = repo_name(owner, repo)
    headers = {name: request.headers[name] for name in GIT_REQUEST_HEADERS if name in request.headers}
    # Pass git's own Accept-Encoding through untouched (httpx would add gzip and the raw
    # relay would hand git bytes it never asked for).
    headers["accept-encoding"] = request.headers.get("accept-encoding", "identity")
    content = request.stream() if request.method == "POST" else None
    upstream = await github.git_send(
        request.method, owner, repo, suffix, params=dict(request.query_params), headers=headers, content=content
    )
    if upstream.status_code in (401, 403, 404):
        await upstream.aclose()
        writing = "git-receive-pack" in (suffix, request.query_params.get("service"))
        # Never let GitHub's 401 through: git would blame our API key and re-prompt.
        status, message = git_refusal(upstream.status_code, owner, repo, writing)
        return Response(message, status_code=status, media_type="text/plain")
    out = {name: upstream.headers[name] for name in GIT_RESPONSE_HEADERS if name in upstream.headers}
    # Raw bytes: the body keeps its upstream Content-Encoding, which we forward as is.
    return StreamingResponse(
        body_of(upstream, raw=True),
        status_code=upstream.status_code,
        headers=out,
        background=BackgroundTask(upstream.aclose),
    )


@app.get("/{owner}/{repo}/info/refs", dependencies=[Depends(require_api_key)], include_in_schema=False)
async def git_info_refs(request: Request, owner: str, repo: str, service: str = Query(default="")) -> Response:
    if service not in GIT_SERVICES:
        # Dumb HTTP protocol: GitHub does not serve it either.
        raise BadRequest("Only smart HTTP is supported (service=git-upload-pack or git-receive-pack)")
    return await relay_git(request, owner, repo, "info/refs")


@app.post("/{owner}/{repo}/{service}", dependencies=[Depends(require_api_key)], include_in_schema=False)
async def git_rpc(request: Request, owner: str, repo: str, service: str) -> Response:
    if service not in GIT_SERVICES:
        raise BadRequest("Unknown git service")
    return await relay_git(request, owner, repo, service)


# --- GitHub-style browsing ---------------------------------------------------------


@app.get("/{owner}/{repo}", dependencies=[Depends(require_api_key)])
async def repo_info(request: Request, owner: str, repo: str) -> dict[str, Any]:
    owner, repo = repo_name(owner, repo)
    raw = await github.api_json(f"/repos/{owner}/{repo}")
    return {"ok": True, "data": compact_repo(raw, public_base(request))}


@app.get("/{owner}/{repo}/branches", dependencies=[Depends(require_api_key)])
async def branches(
    owner: str,
    repo: str,
    limit: int = Query(default=100, ge=1, le=100),
    page: int = Query(default=1, ge=1),
) -> dict[str, Any]:
    owner, repo = repo_name(owner, repo)
    raw = await github.api_json(f"/repos/{owner}/{repo}/branches", {"per_page": limit, "page": page})
    items = [{"name": item["name"], "sha": item["commit"]["sha"], "protected": item.get("protected")} for item in raw]
    return {"ok": True, "data": {"page": page, "count": len(items), "has_more": len(items) == limit, "branches": items}}


@app.get("/{owner}/{repo}/commits", dependencies=[Depends(require_api_key)])
async def commits(
    owner: str,
    repo: str,
    ref: str | None = Query(default=None, description="Branch, tag or SHA. Default branch if omitted."),
    path: str | None = Query(default=None, description="Only commits touching this path."),
    limit: int = Query(default=20, ge=1, le=100),
    page: int = Query(default=1, ge=1),
) -> dict[str, Any]:
    owner, repo = repo_name(owner, repo)
    params: dict[str, Any] = {"per_page": limit, "page": page}
    if ref:
        params["sha"] = check_ref(ref)
    if path:
        params["path"] = path
    raw = await github.api_json(f"/repos/{owner}/{repo}/commits", params)
    items = [
        {
            "sha": item["sha"],
            "message": (item["commit"].get("message") or "").strip(),
            "author": (item["commit"].get("author") or {}).get("name"),
            "date": (item["commit"].get("author") or {}).get("date"),
        }
        for item in raw
    ]
    return {"ok": True, "data": {"page": page, "count": len(items), "has_more": len(items) == limit, "commits": items}}


@app.get("/{owner}/{repo}/commit/{sha:path}", dependencies=[Depends(require_api_key)])
async def commit(owner: str, repo: str, sha: str, patch: bool = Query(default=True)) -> Response:
    """One commit with its changed files, like github.com/<owner>/<repo>/commit/<sha>.
    `<sha>.diff` or `<sha>.patch` returns the whole diff as plain text, as on github.com."""
    owner, repo = repo_name(owner, repo)
    for suffix, accept in ((".diff", "application/vnd.github.diff"), (".patch", "application/vnd.github.patch")):
        if sha.endswith(suffix):
            ref = check_ref(sha[: -len(suffix)])
            upstream = await github.api_stream(
                f"/repos/{owner}/{repo}/commits/{quote(ref, safe='')}", accept=accept
            )
            return stream_back(upstream, media_type="text/plain; charset=utf-8")
    raw = await github.api_json(f"/repos/{owner}/{repo}/commits/{quote(check_ref(sha), safe='')}")
    budget = MAX_COMMIT_PATCH_CHARS
    files = []
    for item in raw.get("files") or []:
        entry: dict[str, Any] = {
            "path": item.get("filename"),
            "status": item.get("status"),
            "additions": item.get("additions"),
            "deletions": item.get("deletions"),
        }
        if item.get("previous_filename"):
            entry["previous_path"] = item["previous_filename"]
        if patch:
            text = item.get("patch")
            if text is None:
                # GitHub leaves it out for binary files and very large diffs.
                entry["patch"] = None
            else:
                keep = min(len(text), MAX_PATCH_CHARS, budget)
                budget -= keep
                entry["patch"] = text[:keep]
                if keep < len(text):
                    entry["patch_truncated"] = True
        files.append(entry)
    info = raw.get("commit") or {}
    author = info.get("author") or {}
    return JSONResponse(
        {
            "ok": True,
            "data": {
                "sha": raw.get("sha"),
                "message": (info.get("message") or "").strip(),
                "author": author.get("name"),
                "date": author.get("date"),
                "parents": [parent.get("sha") for parent in raw.get("parents") or []],
                "stats": raw.get("stats"),
                "files_count": len(files),
                # GitHub lists at most 300 files per commit in this call: `.diff` has them all.
                "files_truncated": len(files) >= 300,
                "files": files,
            },
        }
    )


@app.get("/{owner}/{repo}/tree", dependencies=[Depends(require_api_key)])
@app.get("/{owner}/{repo}/tree/{rest:path}", dependencies=[Depends(require_api_key)])
async def tree(
    owner: str,
    repo: str,
    rest: str = "",
    ref: str | None = Query(default=None, description="Ref containing '/': then the URL path after /tree/ is the directory."),
    recursive: bool = Query(default=False, description="Every file under the directory, not just its direct children."),
) -> dict[str, Any]:
    owner, repo = repo_name(owner, repo)
    cap = settings.gh_max_tree_entries
    if recursive:

        async def fetch_tree(ref: str | None, path: str) -> Any:
            # `<ref>:<dir>` asks GitHub for that subtree only: no whole-repo listing to
            # filter, and its 100k-entry truncation applies to the directory asked for.
            treeish = f"{ref or 'HEAD'}:{path}" if path else (ref or "HEAD")
            return await github.api_json(
                f"/repos/{owner}/{repo}/git/trees/{quote(treeish, safe='')}", {"recursive": "1"}
            )

        ref, path, raw = await first_found(ref_candidates(rest, ref), fetch_tree)
        prefix = f"{path}/" if path else ""
        entries = [
            {"path": prefix + item["path"], "type": "dir" if item["type"] == "tree" else "file", "size": item.get("size")}
            for item in raw.get("tree", [])
            if item["type"] in ("blob", "tree")
        ]
        truncated = bool(raw.get("truncated")) or len(entries) > cap
        return {
            "ok": True,
            "data": {"ref": ref or "HEAD", "path": path, "count": len(entries[:cap]), "truncated": truncated, "entries": entries[:cap]},
        }

    async def fetch_contents(ref: str | None, path: str) -> Any:
        return await github.api_json(f"/repos/{owner}/{repo}/contents/{quote_path(path)}", {"ref": ref} if ref else None)

    ref, path, raw = await first_found(ref_candidates(rest, ref), fetch_contents)
    if isinstance(raw, dict):
        # The path is a file: describe it rather than fail.
        return {"ok": True, "data": {"ref": ref, "path": path, "type": raw.get("type"), "size": raw.get("size")}}
    entries = [{"name": item["name"], "type": item["type"], "size": item.get("size")} for item in raw]
    entries.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
    # GitHub itself stops a directory listing at 1000 entries.
    truncated = len(entries) > cap or len(entries) >= 1000
    return {
        "ok": True,
        "data": {"ref": ref, "path": path, "count": len(entries[:cap]), "truncated": truncated, "entries": entries[:cap]},
    }


@app.get("/{owner}/{repo}/blob/{rest:path}", dependencies=[Depends(require_api_key)])
@app.get("/{owner}/{repo}/raw/{rest:path}", dependencies=[Depends(require_api_key)])
async def raw_file(owner: str, repo: str, rest: str, ref: str | None = Query(default=None)) -> Response:
    owner, repo = repo_name(owner, repo)
    candidates = ref_candidates(rest, ref, need_path=True)
    if not candidates[0][1]:
        raise BadRequest("Expected /blob/<ref>/<path to file>")

    async def fetch(ref: str | None, path: str) -> httpx.Response:
        return await github.api_stream(
            f"/repos/{owner}/{repo}/contents/{quote_path(path)}",
            accept="application/vnd.github.raw",
            params={"ref": ref} if ref else None,
        )

    _, path, upstream = await first_found(candidates, fetch)
    return stream_back(upstream, media_type=raw_media_type(path))


@app.get("/{owner}/{repo}/archive/{name:path}", dependencies=[Depends(require_api_key)])
async def archive(owner: str, repo: str, name: str) -> Response:
    owner, repo = repo_name(owner, repo)
    for suffix, kind, media_type in ((".tar.gz", "tarball", "application/gzip"), (".zip", "zipball", "application/zip")):
        if name.endswith(suffix):
            ref = name[: -len(suffix)]
            break
    else:
        raise BadRequest("Expected /archive/<ref>.tar.gz or /archive/<ref>.zip")
    if not ref:
        raise BadRequest("Invalid ref")
    check_ref(ref)
    # GitHub redirects to codeload with a short-lived token in the URL: the client follows
    # it here instead of handing that URL out. Codeload can take a while to start a big
    # archive, hence the longer read timeout.
    upstream = await github.api_stream(
        f"/repos/{owner}/{repo}/{kind}/{quote(ref, safe='/')}",
        accept="*/*",
        timeout=httpx.Timeout(settings.gh_timeout, read=max(settings.gh_timeout, 300.0)),
    )
    filename = f"{repo}-{ref.replace('/', '-')}{suffix}"
    return stream_back(upstream, media_type=media_type, extra={"Content-Disposition": f'attachment; filename="{filename}"'})
