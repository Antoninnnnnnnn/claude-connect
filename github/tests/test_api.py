"""HTTP layer: auth, the git relay contract, GitHub-style browsing."""

import base64
import gzip

import httpx
import pytest

from app.main import raw_media_type, split_ref_path


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# --- auth ---------------------------------------------------------------------


def test_health_is_public(api):
    del api.headers["X-API-Key"]
    assert api.get("/health").json()["data"] == {"status": "up", "token_configured": True}


def test_missing_key_gets_basic_challenge(api):
    # git only sends the user:key from the clone URL after this challenge.
    del api.headers["X-API-Key"]
    response = api.get("/me/repo/info/refs", params={"service": "git-upload-pack"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic ")


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": basic("x", "test-key")},
        {"Authorization": basic("test-key", "")},
        {"Authorization": "Bearer test-key"},
    ],
)
def test_key_accepted_from_basic_or_bearer(api, upstream, headers):
    upstream.routes[("GET", "/repos/me/repo")] = httpx.Response(200, json={"full_name": "me/repo"})
    del api.headers["X-API-Key"]
    assert api.get("/me/repo", headers=headers).status_code == 200


def test_wrong_key_is_rejected_before_upstream(api, upstream):
    response = api.get("/me/repo", headers={"X-API-Key": "nope", "Authorization": basic("x", "nope")})
    assert response.status_code == 401
    assert upstream.requests == []


def test_invalid_repo_name_never_reaches_upstream(api, upstream):
    assert api.get("/me/re%20po").status_code == 422
    assert upstream.requests == []


# --- git relay ------------------------------------------------------------------


def test_info_refs_relays_protocol_v2_and_swaps_auth(api, upstream):
    advert = b"001e# service=git-upload-pack\n0000"
    upstream.routes[("GET", "/me/repo.git/info/refs")] = httpx.Response(
        200, content=advert, headers={"Content-Type": "application/x-git-upload-pack-advertisement"}
    )
    response = api.get(
        "/me/repo.git/info/refs",
        params={"service": "git-upload-pack"},
        headers={"Git-Protocol": "version=2", "Authorization": basic("x", "test-key")},
    )
    assert response.status_code == 200 and response.content == advert
    assert response.headers["content-type"] == "application/x-git-upload-pack-advertisement"
    sent, _ = upstream.requests[0]
    assert sent.url.params["service"] == "git-upload-pack"
    assert sent.headers["git-protocol"] == "version=2"
    # The caller's credentials are replaced by the GitHub token, never forwarded.
    assert sent.headers["authorization"] == basic("x-access-token", "ghp_test")


def test_upload_pack_relays_gzip_body_and_response_untouched(api, upstream):
    request_body = gzip.compress(b"0032want 0123456789012345678901234567890123456789\n0000")
    pack = gzip.compress(b"PACK" + b"\x00" * 64)

    def handler(request, body):
        assert body == request_body
        return httpx.Response(
            200, content=pack, headers={"Content-Type": "application/x-git-upload-pack-result", "Content-Encoding": "gzip"}
        )

    upstream.routes[("POST", "/me/repo.git/git-upload-pack")] = handler
    response = api.post(
        "/me/repo/git-upload-pack",
        content=request_body,
        headers={
            "Content-Type": "application/x-git-upload-pack-request",
            "Content-Encoding": "gzip",
            "Accept-Encoding": "deflate",
        },
    )
    assert response.status_code == 200
    sent, _ = upstream.requests[0]
    assert sent.headers["content-encoding"] == "gzip"
    # Upstream is asked for git's own encodings, not httpx's defaults.
    assert sent.headers["accept-encoding"] == "deflate"
    assert response.headers["content-encoding"] == "gzip"


def test_receive_pack_is_relayed(api, upstream):
    upstream.routes[("POST", "/me/repo.git/git-receive-pack")] = httpx.Response(200, content=b"000eunpack ok\n0000")
    response = api.post("/me/repo.git/git-receive-pack", content=b"push-data")
    assert response.status_code == 200
    assert upstream.requests[0][1] == b"push-data"


def test_unknown_git_service_is_rejected(api, upstream):
    assert api.get("/me/repo/info/refs", params={"service": "git-upload-archive"}).status_code == 422
    assert api.post("/me/repo/git-upload-archive", content=b"x").status_code == 422
    assert upstream.requests == []


def test_upstream_401_never_reaches_git_as_401(api, upstream):
    # A 401 would make git blame our API key; GitHub sends one for a revoked token.
    upstream.routes[("GET", "/me/repo.git/info/refs")] = httpx.Response(401, headers={"WWW-Authenticate": "Basic"})
    response = api.get("/me/repo/info/refs", params={"service": "git-upload-pack"})
    assert response.status_code == 403
    assert "www-authenticate" not in response.headers
    assert "GITHUB_TOKEN" in response.text


def test_push_without_token_says_so(api, upstream, settings):
    settings.github_token = ""
    upstream.routes[("GET", "/me/repo.git/info/refs")] = httpx.Response(401)
    response = api.get("/me/repo/info/refs", params={"service": "git-receive-pack"})
    assert response.status_code == 403 and "Pushing needs GITHUB_TOKEN" in response.text


def test_missing_repo_is_404(api, upstream):
    response = api.get("/me/ghost/info/refs", params={"service": "git-upload-pack"})
    assert response.status_code == 404 and "not found" in response.text


# --- browsing ---------------------------------------------------------------------


def test_repo_info_gives_clone_url_on_this_host(api, upstream):
    upstream.routes[("GET", "/repos/me/repo")] = httpx.Response(
        200, json={"full_name": "me/repo", "private": True, "default_branch": "main", "permissions": {"push": True}}
    )
    data = api.get("/me/repo.git").json()["data"]
    assert upstream.requests[0][0].headers["authorization"] == "Bearer ghp_test"
    assert data["clone_url"] == "http://testserver/me/repo.git"
    assert data["private"] is True and data["can_push"] is True


def test_blob_streams_raw_content_as_plain_text(api, upstream):
    def handler(request, _):
        assert request.headers["accept"] == "application/vnd.github.raw"
        assert request.url.params["ref"] == "dev"
        return httpx.Response(200, content=b"<script>alert(1)</script>")

    upstream.routes[("GET", "/repos/me/repo/contents/web/index.html")] = handler
    response = api.get("/me/repo/blob/dev/web/index.html")
    assert response.text == "<script>alert(1)</script>"
    # Never rendered as HTML on this domain.
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    # Behind Cloudflare, anything cacheable would be served to callers without the key.
    assert response.headers["cache-control"] == "private, no-store"


def test_blob_ref_with_slash_goes_in_query(api, upstream):
    upstream.routes[("GET", "/repos/me/repo/contents/src/a.py")] = lambda request, _: httpx.Response(
        200, content=request.url.params["ref"].encode()
    )
    assert api.get("/me/repo/blob/src/a.py", params={"ref": "feature/x"}).text == "feature/x"


def test_tree_lists_dirs_first(api, upstream):
    upstream.routes[("GET", "/repos/me/repo/contents/src")] = httpx.Response(
        200,
        json=[
            {"name": "b.py", "type": "file", "size": 3},
            {"name": "lib", "type": "dir", "size": 0},
            {"name": "A.py", "type": "file", "size": 1},
        ],
    )
    data = api.get("/me/repo/tree/main/src").json()["data"]
    assert [entry["name"] for entry in data["entries"]] == ["lib", "A.py", "b.py"]
    assert data["ref"] == "main" and data["path"] == "src"


def test_recursive_tree_caps(api, upstream, settings):
    settings.gh_max_tree_entries = 2
    upstream.routes[("GET", "/repos/me/repo/git/trees/HEAD")] = httpx.Response(
        200,
        json={
            "truncated": False,
            "tree": [
                {"path": "README.md", "type": "blob", "size": 1},
                {"path": "src", "type": "tree"},
                {"path": "src/a.py", "type": "blob", "size": 2},
                {"path": "vendor", "type": "commit"},
            ],
        },
    )
    data = api.get("/me/repo/tree", params={"recursive": True}).json()["data"]
    assert data["count"] == 2 and data["truncated"] is True
    assert [entry["path"] for entry in data["entries"]] == ["README.md", "src"]


def test_recursive_subtree_is_asked_for_directly_and_reprefixed(api, upstream):
    # GitHub answers `<ref>:<dir>` with paths relative to that directory.
    upstream.routes[("GET", "/repos/me/repo/git/trees/main:src/lib")] = httpx.Response(
        200,
        json={"truncated": False, "tree": [{"path": "a.py", "type": "blob", "size": 2}, {"path": "sub", "type": "tree"}]},
    )
    data = api.get("/me/repo/tree/main/src/lib", params={"recursive": True}).json()["data"]
    assert [entry["path"] for entry in data["entries"]] == ["src/lib/a.py", "src/lib/sub"]
    assert data["ref"] == "main" and data["path"] == "src/lib"
    # The tree-ish is one encoded path segment: its slashes cannot climb out.
    assert upstream.requests[0][0].url.raw_path == b"/repos/me/repo/git/trees/main%3Asrc%2Flib?recursive=1"


@pytest.mark.parametrize("ref", ["../../../../user", "main:secret", "a\nb"])
def test_ref_cannot_escape_the_repo(api, upstream, ref):
    # httpx resolves '..' in a URL path: an unchecked ref would reach any GitHub API
    # endpoint with the server's token.
    for url in ("/me/repo/tree", "/me/repo/blob/a.py", "/me/repo/commits"):
        assert api.get(url, params={"ref": ref, "recursive": True}).status_code == 422
    if ref.isprintable():
        assert api.get(f"/me/repo/commit/{ref}").status_code in (404, 422)
    assert upstream.requests == []


def test_branch_with_slash_in_a_github_link_is_found(api, upstream):
    # github.com/<o>/<r>/blob/feature/x/src/a.py: "feature" alone does not exist.
    upstream.routes[("GET", "/repos/me/repo/contents/src/a.py")] = lambda request, _: (
        httpx.Response(200, content=b"ok")
        if request.url.params.get("ref") == "feature/x"
        else httpx.Response(404, json={"message": "Not Found"})
    )
    response = api.get("/me/repo/blob/feature/x/src/a.py")
    assert response.status_code == 200 and response.text == "ok"
    tried = [(sent.url.path, sent.url.params.get("ref")) for sent, _ in upstream.requests]
    assert tried == [("/repos/me/repo/contents/x/src/a.py", "feature"), ("/repos/me/repo/contents/src/a.py", "feature/x")]


def test_missing_file_is_still_404_after_ref_fallback(api, upstream):
    response = api.get("/me/repo/blob/main/a..b/c.txt")
    assert response.status_code == 404 and response.json()["error_code"] == "not_found"
    # "main/a..b" cannot be a ref, so it is not tried.
    assert len(upstream.requests) == 1


def test_tree_with_slashed_branch_and_no_path(api, upstream):
    upstream.routes[("GET", "/repos/me/repo/contents/")] = lambda request, _: (
        httpx.Response(200, json=[{"name": "a", "type": "file", "size": 1}])
        if request.url.params.get("ref") == "feature/x"
        else httpx.Response(404, json={"message": "Not Found"})
    )
    upstream.routes[("GET", "/repos/me/repo/contents/x")] = httpx.Response(404, json={"message": "Not Found"})
    data = api.get("/me/repo/tree/feature/x").json()["data"]
    assert data["ref"] == "feature/x" and data["path"] == "" and data["count"] == 1


def test_archive_follows_codeload_redirect_without_leaking_it(api, upstream):
    upstream.routes[("GET", "/repos/me/repo/tarball/main")] = httpx.Response(
        302, headers={"Location": "https://codeload.test/me/repo/tar.gz/main?token=SECRET"}
    )
    upstream.routes[("GET", "/me/repo/tar.gz/main")] = httpx.Response(200, content=b"tarbytes")
    response = api.get("/me/repo/archive/main.tar.gz")
    assert response.content == b"tarbytes"
    assert "SECRET" not in str(response.headers)
    assert 'filename="repo-main.tar.gz"' in response.headers["content-disposition"]


def test_rate_limit_is_reported(api, upstream):
    upstream.routes[("GET", "/repos/me/repo")] = httpx.Response(403, headers={"x-ratelimit-remaining": "0"})
    response = api.get("/me/repo")
    assert response.status_code == 429 and response.json()["error_code"] == "rate_limited"


def test_upstream_422_is_the_callers_mistake_with_githubs_reason(api, upstream):
    upstream.routes[("GET", "/repos/me/repo/commits")] = httpx.Response(
        422, json={"message": "No commit found for SHA: nope"}
    )
    response = api.get("/me/repo/commits", params={"ref": "nope"})
    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "invalid_request" and "No commit found for SHA: nope" in body["error"]


def test_empty_repository_is_reported_as_such(api, upstream):
    upstream.routes[("GET", "/repos/me/repo/contents/")] = httpx.Response(404, json={"message": "This repository is empty."})
    response = api.get("/me/repo/tree")
    assert response.status_code == 409 and response.json()["error_code"] == "empty_repository"


def test_renamed_repo_redirect_is_followed(api, upstream):
    upstream.routes[("GET", "/repos/me/old")] = httpx.Response(
        301, headers={"Location": "https://api.test/repositories/42"}, json={"message": "Moved Permanently"}
    )
    upstream.routes[("GET", "/repositories/42")] = httpx.Response(200, json={"full_name": "me/new"})
    assert api.get("/me/old").json()["data"]["full_name"] == "me/new"


def test_unknown_endpoint_keeps_the_error_envelope(api):
    response = api.get("/me/repo/pull/3")
    assert response.status_code == 404
    assert response.json()["ok"] is False and response.json()["error_code"] == "unknown_endpoint"


def test_validation_error_has_error_code(api):
    response = api.get("/me/repo/commits", params={"limit": 0})
    assert response.status_code == 422 and response.json()["error_code"] == "invalid_request"


def test_commit_lists_files_with_capped_patch(api, upstream, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "MAX_PATCH_CHARS", 5)
    upstream.routes[("GET", "/repos/me/repo/commits/abc123")] = httpx.Response(
        200,
        json={
            "sha": "abc123",
            "commit": {"message": "Fix it\n", "author": {"name": "Me", "date": "2026-01-01T00:00:00Z"}},
            "parents": [{"sha": "p1"}],
            "stats": {"additions": 2, "deletions": 1, "total": 3},
            "files": [
                {"filename": "a.py", "status": "modified", "additions": 2, "deletions": 1, "patch": "@@ -1 +1 @@ long"},
                {"filename": "b.png", "status": "renamed", "previous_filename": "old.png", "additions": 0, "deletions": 0},
            ],
        },
    )
    data = api.get("/me/repo/commit/abc123").json()["data"]
    assert data["message"] == "Fix it" and data["parents"] == ["p1"] and data["files_count"] == 2
    first, second = data["files"]
    assert first["patch"] == "@@ -1" and first["patch_truncated"] is True
    assert second["patch"] is None and second["previous_path"] == "old.png"
    data = api.get("/me/repo/commit/abc123", params={"patch": False}).json()["data"]
    assert "patch" not in data["files"][0]


def test_commit_diff_is_plain_text(api, upstream):
    def handler(request, _):
        assert request.headers["accept"] == "application/vnd.github.diff"
        return httpx.Response(200, content=b"diff --git a/a b/a")

    upstream.routes[("GET", "/repos/me/repo/commits/abc123")] = handler
    response = api.get("/me/repo/commit/abc123.diff")
    assert response.text == "diff --git a/a b/a"
    assert response.headers["content-type"] == "text/plain; charset=utf-8"


def test_path_traversal_is_rejected(api, upstream):
    assert api.get("/me/repo/blob/main/a/%2E%2E/b").status_code == 422
    assert upstream.requests == []


# --- helpers ------------------------------------------------------------------------


def test_split_ref_path():
    assert split_ref_path("main/src/app.py", None) == ("main", "src/app.py")
    assert split_ref_path("main", None) == ("main", "")
    assert split_ref_path("", None) == (None, "")
    assert split_ref_path("src/app.py", "feature/x") == ("feature/x", "src/app.py")


@pytest.mark.parametrize(
    "path,expected",
    [
        ("a.py", "text/plain; charset=utf-8"),
        ("Makefile", "text/plain; charset=utf-8"),
        ("logo.svg", "text/plain; charset=utf-8"),
        ("page.html", "text/plain; charset=utf-8"),
        ("data.json", "text/plain; charset=utf-8"),
        ("lib.rs", "text/plain; charset=utf-8"),
        ("app.ts", "text/plain; charset=utf-8"),
        ("page.mht", "text/plain; charset=utf-8"),
        ("setup.sql", "text/plain; charset=utf-8"),
        ("photo.png", "image/png"),
        ("doc.pdf", "application/pdf"),
        ("font.woff2", "font/woff2"),
    ],
)
def test_raw_media_type(path, expected):
    assert raw_media_type(path) == expected
