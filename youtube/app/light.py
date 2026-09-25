"""Low-bandwidth track listing: skip the watch page, ask the player API for three fields.

youtube-transcript-api downloads the full watch page (~300 KB compressed) only to read
INNERTUBE_API_KEY, then posts to the innertube player API (~50 KB). The player API answers
without the key, and honours X-Goog-FieldMask: asking only for what the transcript needs
brings the listing to 1-2 KB. Measured 2026-09 on two videos: ~360 KB -> ~5-20 KB per
uncached transcript, the rest being the captions themselves.

This hooks into private internals of the pinned library (TranscriptListFetcher and its
_extract_captions_json). Everything downstream is still the library's: playability
checks, TranscriptList.build, Transcript.fetch, the caption parser and its exceptions.
If YouTube stops honouring this request shape, LightApi falls back to the full library
path inside the same attempt, so a policy change costs bandwidth, not availability.
"""

import logging
from typing import Any

import requests
from youtube_transcript_api import IpBlocked, YouTubeDataUnparsable, YouTubeRequestFailed, YouTubeTranscriptApi
from youtube_transcript_api._settings import INNERTUBE_CONTEXT
from youtube_transcript_api._transcripts import TranscriptList, TranscriptListFetcher
from youtube_transcript_api.proxies import GenericProxyConfig


logger = logging.getLogger(__name__)

PLAYER_URL = "https://www.youtube.com/youtubei/v1/player"
# playabilityStatus and captions stay whole: the library reads nested fields of both
# (errorScreen subreasons, captionTracks names, translationLanguages).
FIELD_MASK = "playabilityStatus,captions,videoDetails(title,author,lengthSeconds)"


class LightPathUnsupported(RuntimeError):
    """YouTube refused or reshaped the masked request: use the full library path."""


class LightFetcher(TranscriptListFetcher):
    def __init__(self, http_client: requests.Session):
        super().__init__(http_client, proxy_config=None)
        self.details: dict[str, Any] = {}

    def _fetch_captions_json(self, video_id: str, try_number: int = 0) -> dict[str, Any]:
        response = self._http_client.post(
            PLAYER_URL,
            json={"context": INNERTUBE_CONTEXT, "videoId": video_id},
            headers={"X-Goog-FieldMask": FIELD_MASK},
        )
        status = response.status_code
        if status == 429:
            raise IpBlocked(video_id)
        if status >= 500:
            raise YouTubeRequestFailed(video_id, requests.HTTPError(f"{status} Server Error"))
        if status >= 400:
            # A new IP would not change a 4xx on the request shape: fall back, don't rotate.
            raise LightPathUnsupported(f"player API answered HTTP {status}")
        try:
            data = response.json()
        except ValueError as exc:
            raise LightPathUnsupported("player API answer is not JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("playabilityStatus"), dict):
            raise LightPathUnsupported("player API answer lacks playabilityStatus")
        details = data.get("videoDetails")
        self.details = details if isinstance(details, dict) else {}
        return self._extract_captions_json(data, video_id)


class LightApi:
    """Duck-types YouTubeTranscriptApi.list(), plus `details` (title, author, length)."""

    def __init__(self, session: requests.Session, proxy: str | None):
        self._session = session
        self._proxy = proxy
        self.details: dict[str, Any] = {}
        self.path: str | None = None
        session.headers.update({"Accept-Language": "en-US"})
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}

    def list(self, video_id: str) -> TranscriptList:
        fetcher = LightFetcher(self._session)
        try:
            tracks = fetcher.fetch(video_id)
            self.details = fetcher.details
            self.path = "light"
            return tracks
        except (LightPathUnsupported, YouTubeDataUnparsable, KeyError, TypeError, IndexError) as exc:
            logger.warning("Light path unusable for %s (%s: %s), falling back to watch page", video_id, type(exc).__name__, exc)
        self.path = "full"
        return full_api_factory(self._session, self._proxy).list(video_id)


def full_api_factory(session: requests.Session, proxy: str | None) -> YouTubeTranscriptApi:
    proxy_config = GenericProxyConfig(http_url=proxy, https_url=proxy) if proxy else None
    return YouTubeTranscriptApi(proxy_config=proxy_config, http_client=session)


def light_api_factory(session: requests.Session, proxy: str | None) -> LightApi:
    return LightApi(session, proxy)
