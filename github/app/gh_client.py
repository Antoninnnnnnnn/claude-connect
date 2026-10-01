import base64
from collections.abc import AsyncIterator, Mapping
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings


class GitHubError(Exception):
    def __init__(self, message: str, *, status: int = 502, code: str = "upstream_error"):
        super().__init__(message)
        self.status = status
        self.code = code


def raise_for_upstream(response: httpx.Response) -> None:
    """Map a GitHub API failure to an error the agent can act on."""
    status = response.status_code
    if status < 400:
        return
    if status == 404:
        # GitHub answers 404, not 403, when the token cannot see a private repo.
        raise GitHubError("Not found, or the token has no access to it", status=404, code="not_found")
    if status == 401:
        raise GitHubError("GitHub rejected GITHUB_TOKEN (expired or revoked)", status=502, code="bad_token")
    if status in (403, 429) and (
        response.headers.get("x-ratelimit-remaining") == "0" or response.headers.get("retry-after")
    ):
        raise GitHubError("GitHub rate limit reached, retry later", status=429, code="rate_limited")
    if status == 403:
        raise GitHubError("GitHub refused: the token lacks the permission", status=403, code="forbidden")
    if status == 409:
        raise GitHubError("Repository is empty", status=409, code="empty_repository")
    raise GitHubError(f"GitHub answered HTTP {status}", status=502, code="upstream_error")


def quote_path(path: str) -> str:
    return quote(path.strip("/"), safe="/")


class GitHubClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        auth = self._auth_headers()
        self.api = httpx.AsyncClient(
            base_url=settings.github_api_url.rstrip("/"),
            timeout=settings.gh_timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "claude-connect-github",
                **auth,
            },
            transport=transport,
        )
        # No read/write timeout: a big clone or push can stay quiet for minutes while
        # GitHub packs objects. Connect still fails fast.
        self.git = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.gh_timeout, read=None, write=None),
            headers=auth,
            transport=transport,
        )

    def _auth_headers(self) -> dict[str, str]:
        token = self.settings.github_token.strip()
        if not token:
            return {}
        # Basic works for both the REST API and git smart HTTP, with classic and fine-grained PATs.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        return {"Authorization": f"Basic {basic}"}

    async def aclose(self) -> None:
        await self.api.aclose()
        await self.git.aclose()

    async def api_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        try:
            response = await self.api.get(path, params=params)
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub unreachable: {type(exc).__name__}") from exc
        raise_for_upstream(response)
        return response.json()

    async def api_stream(
        self, path: str, *, accept: str, params: Mapping[str, Any] | None = None, follow_redirects: bool = False
    ) -> httpx.Response:
        """Open a streamed API response; the caller must aclose() it."""
        request = self.api.build_request("GET", path, params=params, headers={"Accept": accept})
        try:
            response = await self.api.send(request, stream=True, follow_redirects=follow_redirects)
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub unreachable: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            await response.aread()
            await response.aclose()
            raise_for_upstream(response)
        return response

    async def git_send(
        self,
        method: str,
        owner: str,
        repo: str,
        suffix: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        content: AsyncIterator[bytes] | None,
    ) -> httpx.Response:
        """Relay one smart-HTTP request to github.com, streamed; the caller must aclose() it."""
        url = f"{self.settings.github_git_url.rstrip('/')}/{owner}/{repo}.git/{suffix}"
        request = self.git.build_request(method, url, params=params, headers=headers, content=content)
        try:
            return await self.git.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub unreachable: {type(exc).__name__}") from exc
