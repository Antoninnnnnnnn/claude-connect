"""HTTP layer: auth, validation before any upstream call, response shape."""

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import get_settings
from app.yt_client import YouTubeClient
from conftest import FakeApiFactory, FakeSnippet, FakeTrack


VID = "dQw4w9WgXcQ"


@pytest.fixture
def api(settings, monkeypatch):
    factory = FakeApiFactory(
        [
            FakeTrack(
                "en",
                "English",
                snippets=[FakeSnippet(f"line {i} lorem ipsum dolor sit", i * 10.0, 5.0) for i in range(30)],
            )
        ]
    )
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "youtube", YouTubeClient(settings, api_factory=factory))
    main.app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(main.app) as client:
        client.headers["X-API-Key"] = "test-key"
        client.factory = factory
        yield client
    main.app.dependency_overrides.clear()


def test_health_is_public(api):
    del api.headers["X-API-Key"]
    body = api.get("/health").json()
    assert body["ok"] is True and body["data"]["proxy_configured"] is False


def test_api_key_is_required(api):
    response = api.get("/transcript", params={"video": VID}, headers={"X-API-Key": "wrong"})
    assert response.status_code == 401 and response.json()["ok"] is False


def test_bad_video_is_rejected_before_upstream(api):
    response = api.get("/transcript", params={"video": "https://example.com/v"})
    assert response.status_code == 422
    assert response.json()["error_code"] == "invalid_video_id"
    assert api.factory.calls == []


def test_end_before_start_is_rejected(api):
    response = api.get("/transcript", params={"video": VID, "start": 50, "end": 10})
    assert response.status_code == 422


def test_transcript_text_shape(api):
    data = api.get("/transcript", params={"video": f"https://youtu.be/{VID}?t=3", "lang": "en"}).json()["data"]
    assert data["video_id"] == VID and data["language_code"] == "en"
    assert data["text"].startswith("[0:00] line 0 lorem ipsum dolor sit line 1")
    assert data["truncated"] is False and data["duration"] == 295.0
    assert "snippets" not in data and "available_languages" not in data


def test_transcript_paging(api):
    first = api.get("/transcript", params={"video": VID, "max_chars": 500, "paragraph_seconds": 5}).json()["data"]
    assert first["truncated"] is True and first["chars"] <= 500
    second = api.get(
        "/transcript", params={"video": VID, "max_chars": 500, "paragraph_seconds": 5, "start": first["next_start"]}
    ).json()["data"]
    assert second["start"] == first["next_start"] and second["cached"] is True
    assert len(api.factory.calls) == 1


def test_transcript_segments(api):
    data = api.get("/transcript", params={"video": VID, "format": "segments", "end": 20}).json()["data"]
    assert [s["text"] for s in data["segments"]] == ["line 0 lorem ipsum dolor sit", "line 1 lorem ipsum dolor sit"]
    assert data["segments"][1] == {"t": 10.0, "d": 5.0, "text": "line 1 lorem ipsum dolor sit"}


def test_fallback_includes_languages(api):
    data = api.get("/transcript", params={"video": VID, "lang": "fr"}).json()["data"]
    assert data["fallback"] is True
    assert data["available_languages"] == [{"code": "en", "name": "English", "generated": False}]


def test_strict_miss_is_404_with_languages(api):
    response = api.get("/transcript", params={"video": VID, "lang": "fr", "strict": True})
    body = response.json()
    assert response.status_code == 404 and body["ok"] is False
    assert body["error_code"] == "language_not_found" and body["available_languages"]


def test_languages_endpoint(api):
    data = api.get("/languages", params={"video": VID}).json()["data"]
    assert data == {"video_id": VID, "cached": False, "languages": [{"code": "en", "name": "English", "generated": False}]}
