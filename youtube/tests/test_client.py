"""Track choice, proxy rotation, error mapping and cache of the YouTube client."""

import time

import pytest
import requests
from youtube_transcript_api import (
    AgeRestricted,
    IpBlocked,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeRequestFailed,
)

from app.yt_client import TimeoutSession, YouTubeError, map_error, pick_track
from conftest import FakeSnippet, FakeTrack


VID = "dQw4w9WgXcQ"


def en(generated=False):
    return FakeTrack("en", "English (auto-generated)" if generated else "English", is_generated=generated)


# ------------------------------------------------------------------ pick_track


def info(code, generated=False):
    return {"code": code, "name": code, "generated": generated}


def test_pick_exact_language_in_order():
    tracks = [info("en"), info("fr")]
    assert pick_track(tracks, ["fr", "en"], strict=False) == (1, False)


def test_pick_manual_over_generated_same_code():
    tracks = [info("en", generated=True), info("en")]
    assert pick_track(tracks, ["en"], strict=False) == (1, False)


def test_pick_regional_variant_matches_base():
    assert pick_track([info("pt-BR")], ["pt"], strict=False) == (0, False)


def test_pick_exact_code_before_variant():
    tracks = [info("fr-CA"), info("fr", generated=True)]
    assert pick_track(tracks, ["fr"], strict=False) == (1, False)


def test_pick_fallback_prefers_generated_track():
    tracks = [info("ja"), info("es", generated=True)]
    assert pick_track(tracks, ["fr"], strict=False) == (1, True)


def test_pick_fallback_without_generated_takes_first():
    assert pick_track([info("ja"), info("de")], ["fr"], strict=False) == (0, True)


def test_pick_strict_returns_none():
    assert pick_track([info("en")], ["fr"], strict=True) == (None, False)


def test_pick_no_tracks():
    assert pick_track([], ["fr"], strict=False) == (None, False)


# -------------------------------------------------------------------- transcript


def test_transcript_returns_plain_snippets(make_client):
    track = FakeTrack("en", "English", snippets=[FakeSnippet("hi", 1.5, 2.0)])
    client, _ = make_client([track])
    data = client.transcript(VID, languages=["en"])
    assert data["snippets"] == [{"start": 1.5, "duration": 2.0, "text": "hi"}]
    assert data["language_code"] == "en" and data["fallback"] is False and data["cached"] is False
    assert data["available_languages"] == [{"code": "en", "name": "English", "generated": False}]


def test_transcript_default_languages_from_settings(make_client):
    client, _ = make_client([en(), FakeTrack("fr", "French")], yt_default_languages="fr,en")
    assert client.transcript(VID, languages=None)["language_code"] == "fr"


def test_transcript_strict_miss_lists_languages(make_client):
    client, _ = make_client([en()])
    with pytest.raises(YouTubeError) as caught:
        client.transcript(VID, languages=["fr"], strict=True)
    assert caught.value.status == 404 and caught.value.code == "language_not_found"
    assert caught.value.extra["available_languages"][0]["code"] == "en"


def test_transcript_no_tracks(make_client):
    client, _ = make_client([])
    with pytest.raises(YouTubeError) as caught:
        client.transcript(VID, languages=["en"])
    assert caught.value.code == "no_transcripts"


def test_transcript_is_cached(make_client):
    client, factory = make_client([en()])
    client.transcript(VID, languages=["en"])
    again = client.transcript(VID, languages=["en"])
    assert again["cached"] is True and len(factory.calls) == 1


def test_cache_is_shared_across_language_requests(make_client):
    """`lang=fr,en` and `lang=en` resolve to the same track: one upstream fetch."""
    client, factory = make_client([en()])
    client.transcript(VID, languages=["fr", "en"])
    again = client.transcript(VID, languages=["en"])
    assert again["cached"] is True and len(factory.calls) == 1


def test_paging_without_lang_stays_on_same_track(make_client):
    """A page request that drops `lang` must not switch track at the same offsets."""
    client, factory = make_client([en(), FakeTrack("fr", "French")], yt_default_languages="fr,en")
    first = client.transcript(VID, languages=["en"])
    assert first["language_code"] == "en"
    # Default languages would pick French: the agent must repeat `lang`, and when it
    # does the cache answers without a new upstream call.
    assert client.transcript(VID, languages=["en"])["cached"] is True
    assert client.transcript(VID, languages=None)["language_code"] == "fr"
    assert len(factory.calls) == 2


def test_fallback_is_recomputed_on_cache_hit(make_client):
    client, _ = make_client([en()])
    assert client.transcript(VID, languages=["en"])["fallback"] is False
    assert client.transcript(VID, languages=["de"])["fallback"] is True


def test_strict_miss_from_cached_list_skips_upstream(make_client):
    client, factory = make_client([en()])
    client.languages(VID)
    with pytest.raises(YouTubeError) as caught:
        client.transcript(VID, languages=["fr"], strict=True)
    assert caught.value.code == "language_not_found"
    assert len(factory.calls) == 1


def test_transcript_primes_languages_cache(make_client):
    client, factory = make_client([en()])
    client.transcript(VID, languages=["en"])
    assert client.languages(VID)["cached"] is True
    assert len(factory.calls) == 1


# ---------------------------------------------------------------- retry / proxy


def test_no_proxy_is_one_direct_attempt(make_client):
    client, factory = make_client(RequestBlocked(VID))
    with pytest.raises(YouTubeError) as caught:
        client.languages(VID)
    assert caught.value.code == "blocked"
    assert factory.calls == [None]


def test_blocked_rotates_through_proxies(make_client):
    client, factory = make_client(IpBlocked(VID), YouTubeRequestFailed(VID, requests.HTTPError("429")), [en()], yt_proxies="http://p1,http://p2")
    assert client.languages(VID)["languages"][0]["code"] == "en"
    assert factory.calls == ["http://p1", "http://p2", "http://p1"]


def test_network_error_is_retried(make_client):
    client, factory = make_client(requests.ConnectTimeout(), [en()], yt_proxies="http://p1")
    client.languages(VID)
    assert len(factory.calls) == 2


def test_retries_are_capped(make_client):
    client, factory = make_client(RequestBlocked(VID), yt_proxies="http://p1", yt_max_retries=3)
    with pytest.raises(YouTubeError):
        client.languages(VID)
    assert factory.calls == ["http://p1"] * 3


def test_direct_first_then_pool(make_client):
    client, factory = make_client(RequestBlocked(VID), [en()], yt_proxies="http://p1", yt_direct_first=True)
    client.languages(VID)
    assert factory.calls == [None, "http://p1"]


def test_direct_fallback_after_pool(make_client):
    client, factory = make_client(RequestBlocked(VID), yt_proxies="http://p1", yt_max_retries=2, yt_allow_direct_fallback=True)
    with pytest.raises(YouTubeError):
        client.languages(VID)
    assert factory.calls == ["http://p1", "http://p1", None]


def test_pool_start_rotates_between_calls(make_client):
    client, factory = make_client([en()], yt_proxies="http://p1,http://p2", yt_cache_ttl=0)
    client.languages(VID)
    client.languages(VID)
    assert factory.calls == ["http://p1", "http://p2"]


@pytest.mark.parametrize(
    "exc, code",
    [
        (VideoUnavailable(VID), "video_unavailable"),
        (TranscriptsDisabled(VID), "no_transcripts"),
        (AgeRestricted(VID), "age_restricted"),
        (PoTokenRequired(VID), "po_token_required"),
    ],
)
def test_permanent_errors_are_not_retried(make_client, exc, code):
    client, factory = make_client(exc, yt_proxies="http://p1,http://p2")
    with pytest.raises(YouTubeError) as caught:
        client.languages(VID)
    assert caught.value.code == code
    assert len(factory.calls) == 1, "a new IP cannot fix this, it must not cost another proxied call"


def test_fetch_error_after_list_is_mapped(make_client):
    track = FakeTrack("en", "English", fetch_error=PoTokenRequired(VID))
    client, _ = make_client([track])
    with pytest.raises(YouTubeError) as caught:
        client.transcript(VID, languages=["en"])
    assert caught.value.code == "po_token_required"


def test_mapped_messages_stay_short():
    errors = [
        VideoUnavailable(VID),
        TranscriptsDisabled(VID),
        AgeRestricted(VID),
        VideoUnplayable(VID, "Private video", ["sub"]),
        RequestBlocked(VID),
        IpBlocked(VID),
        PoTokenRequired(VID),
        requests.ConnectionError(),
        RuntimeError("?"),
    ]
    for exc in errors:
        mapped = map_error(exc)
        assert len(str(mapped)) < 120 and "github" not in str(mapped).lower()
    assert "Private video" in str(map_error(VideoUnplayable(VID, "Private video", [])))


# ------------------------------------------------------------------------ cache


def test_cache_expires(make_client):
    client, _ = make_client(yt_cache_ttl=0.05)
    client._store("k", {"v": 1})
    time.sleep(0.08)
    assert client._cached("k") is None


def test_cache_evicts_oldest(make_client):
    client, _ = make_client()  # conftest pins max_entries to 3
    for index in range(4):
        client._store(f"k{index}", index)
        time.sleep(0.01)
    assert client._cached("k0") is None and client._cached("k3") == 3


def test_cache_disabled_by_zero_ttl(make_client):
    client, _ = make_client(yt_cache_ttl=0)
    client._store("k", 1)
    assert client._cached("k") is None


def test_timeout_session_sets_default_timeout(monkeypatch):
    seen = {}

    def fake_request(self, method, url, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(requests.Session, "request", fake_request)
    TimeoutSession(7.0).request("GET", "https://example.com")
    assert seen["timeout"] == 7.0
    TimeoutSession(7.0).request("GET", "https://example.com", timeout=2)
    assert seen["timeout"] == 2


def test_timeout_session_caps_to_deadline(monkeypatch):
    seen = {}
    monkeypatch.setattr(requests.Session, "request", lambda self, method, url, **kwargs: seen.update(kwargs))
    TimeoutSession(15.0, time.monotonic() + 2).request("GET", "https://example.com")
    assert seen["timeout"] <= 2


def test_timeout_session_refuses_past_deadline():
    with pytest.raises(requests.Timeout):
        TimeoutSession(15.0, time.monotonic() - 1).request("GET", "https://example.com")


def test_deadline_stops_retries(make_client):
    client, factory = make_client(RequestBlocked(VID), yt_proxies="http://p1", yt_max_retries=50, yt_deadline=1, yt_min_interval=0.3)
    with pytest.raises(YouTubeError):
        client.languages(VID)
    assert len(factory.calls) < 50
