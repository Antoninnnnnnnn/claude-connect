"""Light path: masked player API, and fallback to the full library path."""

import json

import pytest
import requests
from youtube_transcript_api import AgeRestricted, IpBlocked, RequestBlocked, TranscriptsDisabled, YouTubeRequestFailed

import app.light as light
from app.light import FIELD_MASK, LightApi
from app.yt_client import YouTubeClient, YouTubeError


VID = "dQw4w9WgXcQ"
# Exact string the library matches, curly apostrophe included.
BOT_DETECTED = "Sign in to confirm you\u2019re not a bot"
CAPTION_URL = "https://www.youtube.com/api/timedtext?v=dQw4w9WgXcQ&lang=en"
CAPTION_XML = '<?xml version="1.0" encoding="utf-8" ?><transcript><text start="1.5" dur="2.0">hello</text><text start="4.0" dur="1.0">world</text></transcript>'


def player_json(**overrides):
    body = {
        "playabilityStatus": {"status": "OK"},
        "captions": {
            "playerCaptionsTracklistRenderer": {
                "captionTracks": [
                    {"baseUrl": CAPTION_URL, "name": {"runs": [{"text": "English"}]}, "languageCode": "en", "isTranslatable": True},
                    {"baseUrl": CAPTION_URL + "&kind=asr", "name": {"runs": [{"text": "English (auto-generated)"}]}, "languageCode": "en", "kind": "asr"},
                ],
                "translationLanguages": [{"languageCode": "fr", "languageName": {"runs": [{"text": "French"}]}}],
            }
        },
        "videoDetails": {"title": "Never Gonna", "author": "Rick", "lengthSeconds": "212"},
    }
    body.update(overrides)
    return body


def make_response(status, payload=None, text=None, url=""):
    response = requests.Response()
    response.status_code = status
    response.reason = "OK" if status < 400 else "Error"
    response.url = url
    response._content = (json.dumps(payload) if payload is not None else text or "").encode()
    return response


class FakeSession(requests.Session):
    def __init__(self, post_response):
        super().__init__()
        self.post_response = post_response
        self.posts = []
        self.gets = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.post_response

    def get(self, url, **kwargs):
        self.gets.append(url)
        return make_response(200, text=CAPTION_XML, url=url)


def test_masked_request_shape():
    session = FakeSession(make_response(200, player_json()))
    LightApi(session, None).list(VID)
    url, kwargs = session.posts[0]
    assert url == light.PLAYER_URL and "key=" not in url
    assert kwargs["headers"]["X-Goog-FieldMask"] == FIELD_MASK
    assert kwargs["json"]["videoId"] == VID
    assert session.headers["Accept-Language"] == "en-US"
    assert not any("watch" in get for get in session.gets), "light path must not download the watch page"


def test_light_path_lists_tracks_and_details():
    session = FakeSession(make_response(200, player_json()))
    api = LightApi(session, "http://proxy:1")
    tracks = list(api.list(VID))
    assert [(t.language_code, t.is_generated) for t in tracks] == [("en", False), ("en", True)]
    assert api.path == "light"
    assert api.details == {"title": "Never Gonna", "author": "Rick", "lengthSeconds": "212"}
    assert session.proxies == {"http": "http://proxy:1", "https": "http://proxy:1"}
    fetched = tracks[0].fetch()
    assert [(s.text, s.start) for s in fetched.snippets] == [("hello", 1.5), ("world", 4.0)]


@pytest.mark.parametrize(
    "playability, error",
    [
        ({"status": "LOGIN_REQUIRED", "reason": BOT_DETECTED}, RequestBlocked),
        ({"status": "LOGIN_REQUIRED", "reason": "This video may be inappropriate for some users."}, AgeRestricted),
    ],
)
def test_library_playability_checks_still_apply(playability, error):
    session = FakeSession(make_response(200, player_json(playabilityStatus=playability)))
    with pytest.raises(error):
        LightApi(session, None).list(VID)


def test_no_captions_is_transcripts_disabled():
    session = FakeSession(make_response(200, player_json(captions={})))
    with pytest.raises(TranscriptsDisabled):
        LightApi(session, None).list(VID)


def test_429_is_a_block_not_a_fallback(monkeypatch):
    monkeypatch.setattr(light, "full_api_factory", lambda *a: pytest.fail("must not fall back on 429"))
    with pytest.raises(IpBlocked):
        LightApi(FakeSession(make_response(429, {})), None).list(VID)


def test_5xx_is_retryable_not_a_fallback(monkeypatch):
    monkeypatch.setattr(light, "full_api_factory", lambda *a: pytest.fail("must not fall back on 5xx"))
    with pytest.raises(YouTubeRequestFailed):
        LightApi(FakeSession(make_response(503, {})), None).list(VID)


class FullStub:
    def __init__(self):
        self.calls = []

    def __call__(self, session, proxy):
        self.calls.append(proxy)
        return self

    def list(self, video_id):
        return ["full-path-result"]


@pytest.mark.parametrize(
    "response",
    [
        make_response(400, {"error": "bad mask"}),
        make_response(403, {}),
        make_response(200, text="<html>not json</html>"),
        make_response(200, ["unexpected"]),
        make_response(200, {"captions": {}}),  # no playabilityStatus
        make_response(200, player_json(captions={"playerCaptionsTracklistRenderer": {"captionTracks": [{"languageCode": "en"}]}})),
    ],
)
def test_structural_failures_fall_back_to_full_path(monkeypatch, response):
    stub = FullStub()
    monkeypatch.setattr(light, "full_api_factory", stub)
    api = LightApi(FakeSession(response), "http://proxy:1")
    assert api.list(VID) == ["full-path-result"]
    assert api.path == "full" and stub.calls == ["http://proxy:1"], "fallback must reuse the same route"


def test_client_uses_video_details_without_oembed(settings, monkeypatch):
    settings.yt_fetch_title = True
    monkeypatch.setattr(YouTubeClient, "_oembed", lambda *a: pytest.fail("oEmbed must not run when videoDetails has a title"))
    sessions = []

    def factory(session, proxy):
        fake = FakeSession(make_response(200, player_json()))
        sessions.append(fake)
        return LightApi(fake, proxy)

    data = YouTubeClient(settings, api_factory=factory).transcript(VID, languages=["en"])
    assert data["title"] == "Never Gonna" and data["channel"] == "Rick" and data["length_seconds"] == 212.0
    assert [s["text"] for s in data["snippets"]] == ["hello", "world"]


def test_client_light_block_rotates_proxy(settings):
    settings.yt_proxies = "http://p1,http://p2"
    routes = []

    def factory(session, proxy):
        routes.append(proxy)
        body = player_json() if len(routes) > 1 else player_json(playabilityStatus={"status": "LOGIN_REQUIRED", "reason": BOT_DETECTED})
        return LightApi(FakeSession(make_response(200, body)), proxy)

    data = YouTubeClient(settings, api_factory=factory).transcript(VID, languages=["en"])
    assert routes == ["http://p1", "http://p2"] and data["language_code"] == "en"


def test_client_permanent_error_on_light_path_is_not_retried(settings):
    settings.yt_proxies = "http://p1,http://p2"
    routes = []

    def factory(session, proxy):
        routes.append(proxy)
        return LightApi(FakeSession(make_response(200, player_json(captions={}))), proxy)

    with pytest.raises(YouTubeError) as caught:
        YouTubeClient(settings, api_factory=factory).transcript(VID, languages=["en"])
    assert caught.value.code == "no_transcripts" and routes == ["http://p1"]


def test_light_mode_setting_selects_factory(settings):
    settings.yt_light_mode = True
    assert YouTubeClient(settings)._api_factory is light.light_api_factory
    settings.yt_light_mode = False
    assert YouTubeClient(settings)._api_factory is light.full_api_factory
