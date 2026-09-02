"""Opt-in contract tests against the running services.

The unit tests in each service pin *our* parsing against fixtures. They cannot
notice that Leboncoin renamed a field or that La Centrale changed its markup:
the parser keeps returning None and the API keeps answering 200 with thinner
data. These canaries close that gap by asserting that a real response still
carries the fields the LLM docs promise.

They need the services up and the real .env, so they are deselected by default:

    pytest tests_live -m live

Set LIVE_BASE_<SERVICE> to point at another host.
"""

import os
from pathlib import Path

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]

SERVICES = {
    "vinted": ("LIVE_BASE_VINTED", "http://127.0.0.1:8091"),
    "leboncoin": ("LIVE_BASE_LEBONCOIN", "http://127.0.0.1:8092"),
    "ecoledirecte": ("LIVE_BASE_ECOLEDIRECTE", "http://127.0.0.1:8093"),
    "lacentrale": ("LIVE_BASE_CENTRALE", "http://127.0.0.1:8094"),
}


def pytest_configure(config):
    config.addinivalue_line("markers", "live: hits real upstream services (opt-in)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("-m") == "live":
        return
    skip = pytest.mark.skip(reason="live canary: run with `-m live`")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


def _load_env() -> dict[str, str]:
    """Read the shared .env without importing any service package."""
    values: dict[str, str] = {}
    env_path = WORKSPACE_ROOT / ".env"
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


@pytest.fixture(scope="session")
def api_key() -> str:
    key = os.environ.get("API_KEY") or _load_env().get("API_KEY")
    if not key:
        pytest.skip("API_KEY not available")
    return key


@pytest.fixture(scope="session")
def bases() -> dict[str, str]:
    return {
        name: os.environ.get(env_var, default)
        for name, (env_var, default) in SERVICES.items()
    }


@pytest.fixture(scope="session")
def http():
    httpx = pytest.importorskip("httpx")
    with httpx.Client(timeout=90.0) as session:
        yield session


@pytest.fixture
def call(http, api_key, bases):
    def _call(service: str, path: str, allow_errors: bool = False, **params):
        """GET an endpoint with the API key.

        A 5xx normally means the upstream is down rather than that our parsing
        broke, so the test skips instead of failing. Pass `allow_errors=True`
        when the error status is the thing being asserted (this API returns 502
        for a refused upstream URL, which is a correct answer, not an outage).
        """
        response = http.get(
            f"{bases[service]}{path}",
            params=params or None,
            headers={"X-API-Key": api_key},
        )
        if not allow_errors and response.status_code >= 500:
            pytest.skip(f"{service}{path} unavailable: HTTP {response.status_code}")
        return response

    return _call
