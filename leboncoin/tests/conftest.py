import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings  # noqa: E402
from app.lbc_client import LeboncoinClient  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    """Settings that never touch the real .env, so tests are hermetic."""
    return Settings(
        _env_file=None,
        API_KEY="test-key",
        LBC_PROXIES="",
        LBC_PROXY=None,
        DECODO_PROXY=None,
        DATAIMPULSE_PROXY=None,
        EVOMI_PROXY=None,
        VINTED_PROXY=None,
        LBC_CACHE_TTL=60,
        LBC_CACHE_MAX_ENTRIES=3,
        LBC_MIN_INTERVAL=0,
    )


@pytest.fixture
def client(settings: Settings) -> LeboncoinClient:
    return LeboncoinClient(settings)
