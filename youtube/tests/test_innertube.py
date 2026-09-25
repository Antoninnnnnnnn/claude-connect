"""Search/browse parsing on trimmed real innertube answers (tests/fixtures, captured 2026-09).

The fixtures keep YouTube's real nesting, cut to a few items per list. These tests pin
our parsing; the live canaries catch YouTube reshaping the pages.
"""

import json
from pathlib import Path

import pytest

from app import innertube
from app.innertube import (
    InvalidReference,
    decode_cursor,
    encode_cursor,
    parse_channel_info,
    parse_channel_ref,
    parse_items,
    parse_playlist_id,
    parse_playlist_info,
    parse_video,
    search_params,
    sort_chip_tokens,
)
from app.yt_client import YouTubeClient, YouTubeError


FIXTURES = Path(__file__).parent / "fixtures"
CHANNEL_ID = "UCYO_jab_esuFRV4b17AJtAw"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------- parsing


def test_search_mixes_old_and_new_renderers_and_skips_shelves():
    items, token = parse_items(load("search_mixed"))
    kinds = {item["type"] for item in items}
    assert kinds == {"video", "playlist"}
    assert token
    videos = [item for item in items if item["type"] == "video"]
    assert all(set(video) >= {"id", "title", "channel", "duration", "views", "published", "url"} for video in videos)
    # Collaborations list two channels and carry no single channel_id; the rest do.
    assert sum("channel_id" in video for video in videos) >= len(videos) // 2
    assert videos[0]["url"] == f"https://www.youtube.com/watch?v={videos[0]['id']}"
    playlist = next(item for item in items if item["type"] == "playlist")
    assert playlist["video_count"]  # "18 videos", or "9 lessons" for a course
    assert playlist["url"].startswith("https://www.youtube.com/playlist?list=")


def test_search_continuation_page_is_parsed():
    items, token = parse_items(load("search_page2"))
    assert items and {item["type"] for item in items} <= {"video", "playlist"}
    assert token


def test_search_channel_results_read_handle_and_subscribers_by_content():
    items, _ = parse_items(load("search_channels"))
    channel = items[0]
    assert channel["type"] == "channel"
    assert channel["handle"].startswith("@")
    assert "subscribers" in channel["subscribers"]
    assert channel["url"] == f"https://www.youtube.com/{channel['handle']}"


def test_channel_videos_lockups_carry_duration_from_the_thumbnail_badge():
    payload = load("channel_videos")
    items, token = parse_items(payload)
    assert items and all(item["type"] == "video" and ":" in item["duration"] for item in items)
    assert token
    assert len(sort_chip_tokens(payload)) == 3


@pytest.mark.parametrize("name", ["channel_page2", "channel_popular", "playlist"])
def test_browse_continuations_and_playlist_pages(name):
    items, token = parse_items(load(name))
    assert items and all(item["type"] == "video" for item in items)
    assert token


def test_shorts_and_channel_playlists_tab():
    shorts, _ = parse_items(load("channel_shorts"))
    assert shorts and shorts[0]["type"] == "short"
    assert shorts[0]["url"] == f"https://www.youtube.com/shorts/{shorts[0]['id']}"
    playlists, _ = parse_items(load("channel_playlists"))
    assert playlists and all(item["type"] == "playlist" for item in playlists)


def test_unknown_renderers_are_skipped_not_fatal():
    payload = {
        "onResponseReceivedActions": [
            {
                "appendContinuationItemsAction": {
                    "continuationItems": [
                        {"brandNewRenderer": {"videoId": "x"}},
                        {"richItemRenderer": {"content": {"lockupViewModel": {"contentType": "LOCKUP_CONTENT_TYPE_ALBUM", "contentId": "y"}}}},
                        {"richItemRenderer": {"content": {"lockupViewModel": {"contentType": "LOCKUP_CONTENT_TYPE_VIDEO", "contentId": "abcdefghijk"}}}},
                        {"continuationItemRenderer": {"continuationEndpoint": {"continuationCommand": {"token": "T2"}}}},
                    ]
                }
            }
        ]
    }
    items, token = parse_items(payload)
    assert items == [{"type": "video", "id": "abcdefghijk", "url": "https://www.youtube.com/watch?v=abcdefghijk"}]
    assert token == "T2"


def test_channel_info_and_missing_channel():
    info = parse_channel_info(load("channel_videos"))
    assert info["id"] == CHANNEL_ID and info["handle"] == "@3blue1brown"
    assert "subscribers" in info["subscribers"] and "videos" in info["video_count"]
    assert parse_channel_info(load("channel_missing")) is None


def test_playlist_info():
    info = parse_playlist_info(load("playlist"), "PLZHQObOWTQDNU6R1_67000Dx_ZCJB-3pi")
    assert info["title"] == "Neural networks" and info["channel_id"] == CHANNEL_ID


def test_video_metadata_from_web_player_even_when_unplayable():
    data = parse_video(load("player"))
    assert data["id"] == "aircAruvnKk" and data["channel_id"] == CHANNEL_ID
    assert data["duration_seconds"] == 1120 and isinstance(data["views"], int)
    assert data["published"].startswith("2017-10-05")
    assert parse_video({"playabilityStatus": {"status": "ERROR"}}) is None


# ---------------------------------------------------------- request building


def test_search_params_match_youtube_filter_values():
    # Values youtube.com itself sends, checked live 2026-09.
    assert search_params(type="video") == "EgIQAQ=="
    assert search_params(type="channel") == "EgIQAg=="
    assert search_params(type="video", duration="long") == "EgQQARgC"
    assert search_params(type="video", upload="week") == "EgQIAxAB"
    assert search_params(type="video", sort="views") == "CAMSAhAB"
    assert search_params(sort="relevance") is None
    assert search_params() is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("UCYO_jab_esuFRV4b17AJtAw", ("id", CHANNEL_ID)),
        ("https://www.youtube.com/channel/UCYO_jab_esuFRV4b17AJtAw/videos", ("id", CHANNEL_ID)),
        ("@3blue1brown", ("url", "https://www.youtube.com/@3blue1brown")),
        ("3blue1brown", ("url", "https://www.youtube.com/@3blue1brown")),
        ("youtube.com/@3blue1brown/shorts", ("url", "https://www.youtube.com/@3blue1brown")),
        ("https://m.youtube.com/c/foo", ("url", "https://www.youtube.com/c/foo")),
        ("https://www.youtube.com/user/foo", ("url", "https://www.youtube.com/user/foo")),
    ],
)
def test_parse_channel_ref(value, expected):
    assert parse_channel_ref(value) == expected


@pytest.mark.parametrize("value", ["", "https://example.com/@foo", "https://www.youtube.com/watch?v=aircAruvnKk", "a b"])
def test_parse_channel_ref_rejects(value):
    with pytest.raises(InvalidReference):
        parse_channel_ref(value)


def test_parse_playlist_id():
    assert parse_playlist_id("PLZHQObOWTQDNU6R1_67000Dx_ZCJB-3pi") == "PLZHQObOWTQDNU6R1_67000Dx_ZCJB-3pi"
    url = "https://www.youtube.com/watch?v=aircAruvnKk&list=PLZHQObOWTQDNU6R1_67000Dx_ZCJB-3pi"
    assert parse_playlist_id(url) == "PLZHQObOWTQDNU6R1_67000Dx_ZCJB-3pi"
    with pytest.raises(InvalidReference):
        parse_playlist_id("https://www.youtube.com/watch?v=aircAruvnKk")


def test_cursor_round_trip_and_garbage():
    assert decode_cursor(encode_cursor("tok", 7)) == ("tok", 7)
    assert decode_cursor(encode_cursor(None, 3)) == (None, 3)
    with pytest.raises(InvalidReference):
        decode_cursor("not-a-cursor")


# ----------------------------------------------------------------- pagination


def page(ids: list[str], token: str | None) -> dict:
    items = [{"videoRenderer": {"videoId": video_id}} for video_id in ids]
    if token:
        items.append({"continuationItemRenderer": {"continuationEndpoint": {"continuationCommand": {"token": token}}}})
    return {"onResponseReceivedCommands": [{"appendContinuationItemsAction": {"continuationItems": items}}]}


@pytest.fixture
def paged_client(settings, monkeypatch):
    pages = {
        None: page(["a1", "a2", "a3"], "P2"),
        "P2": page(["b1", "b2", "b3"], "P3"),
        "P3": page(["c1"], None),
    }
    calls: list[str | None] = []

    def fake(self, endpoint, body, label, headers=None):
        token = body.get("continuation")
        calls.append(token)
        return pages[token]

    monkeypatch.setattr(YouTubeClient, "_innertube", fake)
    client = YouTubeClient(settings)
    client.calls = calls
    return client


def run(client, limit, cursor=None):
    return client.search("q", type="video", duration=None, upload=None, sort=None, limit=limit, cursor=cursor)


def test_limit_cuts_mid_page_and_next_resumes_without_gap(paged_client):
    first = run(paged_client, 2)
    assert [item["id"] for item in first["items"]] == ["a1", "a2"]
    second = run(paged_client, 3, first["next"])
    assert [item["id"] for item in second["items"]] == ["a3", "b1", "b2"]
    third = run(paged_client, 10, second["next"])
    assert [item["id"] for item in third["items"]] == ["b3", "c1"]
    assert third["next"] is None


def test_limit_spans_pages_and_stops_at_the_end(paged_client):
    data = run(paged_client, 100)
    assert data["count"] == 7 and data["next"] is None
    assert paged_client.calls == [None, "P2", "P3"]


def test_page_boundary_cut_points_at_the_next_page(paged_client):
    data = run(paged_client, 3)
    assert data["count"] == 3
    assert decode_cursor(data["next"]) == ("P2", 0)
    assert paged_client.calls == [None]


def test_max_pages_bounds_one_call(paged_client, settings):
    settings.yt_max_pages = 2
    data = run(paged_client, 100)
    assert data["count"] == 6 and decode_cursor(data["next"]) == ("P3", 0)


# ------------------------------------------------------------ client errors


class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_innertube_status_mapping(settings, monkeypatch):
    responses: list[FakeResponse] = []
    monkeypatch.setattr("app.yt_client.TimeoutSession.post", lambda self, *a, **k: responses.pop(0))
    client = YouTubeClient(settings)

    responses[:] = [FakeResponse(400)]
    with pytest.raises(YouTubeError) as exc:
        client._innertube("search", {"query": "a"}, "t")
    assert exc.value.code == "upstream_rejected"

    responses[:] = [FakeResponse(429)]
    with pytest.raises(YouTubeError) as exc:
        client._innertube("search", {"query": "b"}, "t")
    assert exc.value.code == "blocked"

    responses[:] = [FakeResponse(200, {"ok": 1})]
    assert client._innertube("search", {"query": "c"}, "t") == {"ok": 1}
    # Second identical call is a cache hit: no response left to pop.
    assert client._innertube("search", {"query": "c"}, "t") == {"ok": 1}


def test_browse_cache_does_not_evict_transcripts(settings):
    client = YouTubeClient(settings)
    client._store(("languages", "v"), {"languages": []})
    for index in range(settings.yt_browse_cache_max_entries + 5):
        client._store(("innertube", index), {}, browse=True)
    assert client._cached(("languages", "v")) == {"languages": []}


def test_channel_sort_uses_chip_and_fills_channel_id(settings, monkeypatch):
    first_page = load("channel_videos")
    popular = load("channel_popular")
    chips = sort_chip_tokens(first_page)
    seen: list[dict] = []

    def fake(self, endpoint, body, label, headers=None):
        seen.append(body)
        if body.get("continuation") == chips[1]:
            return popular
        return first_page

    monkeypatch.setattr(YouTubeClient, "_innertube", fake)
    data = YouTubeClient(settings).channel(CHANNEL_ID, tab="videos", sort="popular", limit=2, cursor=None)
    assert data["channel"]["handle"] == "@3blue1brown"
    assert data["count"] == 2 and all(item["channel_id"] == CHANNEL_ID for item in data["items"])
    assert seen[0]["params"] == innertube.CHANNEL_TABS["videos"] and seen[1] == {"continuation": chips[1]}
