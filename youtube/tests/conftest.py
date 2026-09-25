import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings  # noqa: E402
from app.yt_client import YouTubeClient  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    """Settings that never touch the real .env, so tests are hermetic."""
    return Settings(
        _env_file=None,
        API_KEY="test-key",
        YT_PROXIES="",
        YT_PROXY=None,
        DECODO_PROXY=None,
        DATAIMPULSE_PROXY=None,
        EVOMI_PROXY=None,
        YT_MIN_INTERVAL=0,
        YT_FETCH_TITLE=False,
        YT_CACHE_TTL=60,
        YT_CACHE_MAX_ENTRIES=3,
        YT_MAX_RETRIES=3,
    )


@dataclass
class FakeSnippet:
    text: str
    start: float
    duration: float


@dataclass
class FakeFetched:
    snippets: list[FakeSnippet]


@dataclass
class FakeTrack:
    language_code: str
    language: str
    is_generated: bool = False
    is_translatable: bool = True
    snippets: list[FakeSnippet] = field(default_factory=lambda: [FakeSnippet("hello", 0.0, 2.0)])
    fetch_error: Exception | None = None

    def fetch(self) -> FakeFetched:
        if self.fetch_error:
            raise self.fetch_error
        return FakeFetched(self.snippets)


class FakeApiFactory:
    """Stands in for YouTubeTranscriptApi. `outcomes` is consumed one per attempt:
    an Exception is raised from list(), a list of tracks is returned."""

    def __init__(self, *outcomes: Any):
        self.outcomes = list(outcomes)
        self.calls: list[str | None] = []

    def __call__(self, session: Any, proxy: str | None) -> "FakeApiFactory":
        self.calls.append(proxy)
        return self

    def list(self, video_id: str) -> list[FakeTrack]:
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def make_client(settings: Settings):
    def _make(*outcomes: Any, **overrides: Any) -> tuple[YouTubeClient, FakeApiFactory]:
        for key, value in overrides.items():
            setattr(settings, key, value)
        factory = FakeApiFactory(*outcomes)
        return YouTubeClient(settings, api_factory=factory), factory

    return _make
