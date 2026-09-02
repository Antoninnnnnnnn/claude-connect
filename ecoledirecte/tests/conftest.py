import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import Settings  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        API_KEY="test-key",
        ED_USERNAME="test-user",
        ED_PASSWORD="test-pass",
        ED_QCM_FILE=str(tmp_path / "qcm.json"),
        ED_LOGIN_BACKOFF_BASE=10,
        ED_LOGIN_BACKOFF_MAX=100,
        ED_MAX_CREDENTIAL_FAILURES=3,
    )


@pytest.fixture
def manager(settings):
    from ed_session import EDSessionManager

    return EDSessionManager(settings)
