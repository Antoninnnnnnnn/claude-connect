import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.main as main  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.gh_client import GitHubClient  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    """Settings that never touch the real .env, so tests are hermetic."""
    return Settings(
        _env_file=None,
        API_KEY="test-key",
        GITHUB_TOKEN="ghp_test",
        GH_API_URL="https://api.test",
        GH_GIT_URL="https://git.test",
    )


class Chunks(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data


def streamed(response: httpx.Response) -> httpx.Response:
    # A Response built from bytes counts as already read, which aiter_raw() refuses:
    # rebuild it the way a real network response arrives, body still encoded.
    return httpx.Response(response.status_code, headers=response.headers, stream=Chunks(b"".join(response.stream)))


class Upstream:
    """Fake GitHub. `routes` maps (method, path) to a handler or a Response; every
    request is recorded with its body so tests can check what was relayed."""

    def __init__(self):
        self.routes: dict[tuple[str, str], object] = {}
        self.requests: list[tuple[httpx.Request, bytes]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        self.requests.append((request, body))
        handler = self.routes.get((request.method, request.url.path))
        if handler is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if callable(handler):
            handler = handler(request, body)
        return streamed(handler)


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
def api(settings, upstream, monkeypatch):
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "github", GitHubClient(settings, transport=httpx.MockTransport(upstream)))
    main.app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(main.app) as client:
        client.headers["X-API-Key"] = "test-key"
        yield client
    main.app.dependency_overrides.clear()
