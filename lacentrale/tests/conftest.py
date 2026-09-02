import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.centrale_client import DISTANCE_UI_TO_API, CentraleClient  # noqa: E402
from app.config import Settings  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Hermetic settings: no real .env, no shared cookie/token files, no browser."""
    return Settings(
        _env_file=None,
        API_KEY="test-key",
        CENTRALE_PROXY=None,
        CENTRALE_PROXIES="",
        LBC_PROXY=None,
        LBC_PROXIES="",
        DECODO_PROXY=None,
        DATAIMPULSE_PROXY=None,
        EVOMI_PROXY=None,
        VINTED_PROXY=None,
        CENTRALE_MIN_INTERVAL=0,
        CENTRALE_COOKIE_FILE=str(tmp_path / "cookies.json"),
        CENTRALE_DATADOME_TOKEN_FILE=str(tmp_path / "token.json"),
        CENTRALE_BROWSER_ENABLED=False,
        CENTRALE_UPSTREAM_API_KEY="test-upstream-key",
        CENTRALE_CACHE_MAX_ENTRIES=3,
    )


@pytest.fixture
def client(settings: Settings) -> CentraleClient:
    instance = CentraleClient(settings)
    # Pin the distance buckets: resolving them lazily would hit the network.
    instance._distance_buckets = dict(DISTANCE_UI_TO_API)
    return instance
