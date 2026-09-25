import logging
import threading
import time
from typing import Any, Callable

import requests
from youtube_transcript_api import (
    AgeRestricted,
    CouldNotRetrieveTranscript,
    FailedToCreateConsentCookie,
    InvalidVideoId,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeDataUnparsable,
    YouTubeRequestFailed,
)

from app.config import Settings
from app.light import full_api_factory, light_api_factory
from app.video_id import watch_url


logger = logging.getLogger(__name__)

OEMBED_URL = "https://www.youtube.com/oembed"

# A new exit IP can fix these: YouTube flagged the IP, or the proxy itself failed.
RETRYABLE = (RequestBlocked, YouTubeRequestFailed, YouTubeDataUnparsable, FailedToCreateConsentCookie, requests.RequestException)


class YouTubeError(RuntimeError):
    """Upstream failure mapped to a short message the agent can act on.

    The library's own messages run several paragraphs with GitHub referral links:
    pure context noise for an LLM, so they never reach the response.
    """

    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.extra = extra


class TimeoutSession(requests.Session):
    """requests.Session with a default timeout: the library never passes one.

    One attempt makes 3-4 requests (watch page, innertube, captions, oEmbed), so a
    per-request timeout alone does not bound the call. `deadline` (monotonic) caps
    every request to the time left for the whole call.
    """

    def __init__(self, timeout: float, deadline: float | None = None):
        super().__init__()
        self._timeout = timeout
        self._deadline = deadline

    def request(self, method, url, **kwargs):  # type: ignore[override]
        timeout = kwargs.get("timeout") or self._timeout
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise requests.Timeout("call deadline exceeded")
            timeout = min(timeout, remaining)
        kwargs["timeout"] = timeout
        return super().request(method, url, **kwargs)


def track_info(transcript: Any) -> dict[str, Any]:
    return {
        "code": transcript.language_code,
        "name": transcript.language,
        "generated": bool(transcript.is_generated),
    }


def base_language(code: str) -> str:
    return code.split("-", 1)[0].lower()


def pick_track(tracks: list[dict[str, Any]], languages: list[str], strict: bool) -> tuple[int | None, bool]:
    """Choose the track to fetch among `track_info` dicts. Returns (index, fallback_used).

    For each requested language, in order: exact code before regional variant
    (`fr` matches `fr-FR`, `pt` matches `pt-BR`), manual before auto-generated.
    Without `strict`, a video with none of the requested languages still returns
    something: its auto-generated track first, since ASR follows the spoken
    language while manual tracks are often third-party translations.
    """
    for wanted in languages:
        wanted_lower = wanted.lower()
        matches = [index for index, track in enumerate(tracks) if base_language(track["code"]) == base_language(wanted_lower)]
        if matches:
            matches.sort(key=lambda index: (tracks[index]["code"].lower() != wanted_lower, tracks[index]["generated"]))
            return matches[0], False
    if strict or not tracks:
        return None, False
    generated = [index for index, track in enumerate(tracks) if track["generated"]]
    return (generated or [0])[0], True


def language_miss(tracks: list[dict[str, Any]], wanted: list[str]) -> "YouTubeError":
    if not tracks:
        return YouTubeError(404, "no_transcripts", "This video has no subtitles.")
    return YouTubeError(
        404,
        "language_not_found",
        f"No subtitles in {','.join(wanted)}. Retry with one of available_languages.",
        available_languages=tracks,
    )


class YouTubeClient:
    def __init__(self, settings: Settings, api_factory: Callable[[requests.Session, str | None], Any] | None = None):
        self.settings = settings
        self._api_factory = api_factory or (light_api_factory if settings.yt_light_mode else full_api_factory)
        self._lock = threading.Lock()
        # Pacing gets its own lock so a cache hit never queues behind a throttle sleep.
        self._throttle_lock = threading.Lock()
        self._last_request = 0.0
        self._proxy_index = 0
        self._cache: dict[Any, tuple[float, Any]] = {}

    # ------------------------------------------------------------------ public

    def languages(self, video_id: str) -> dict[str, Any]:
        cached = self._cached(("languages", video_id))
        if cached is not None:
            return {"video_id": video_id, "cached": True, **cached}

        def work(api: Any, session: requests.Session) -> dict[str, Any]:
            return {"languages": [track_info(track) for track in api.list(video_id)]}

        data = self._with_retries(video_id, work)
        self._store(("languages", video_id), data)
        return {"video_id": video_id, "cached": False, **data}

    def transcript(self, video_id: str, *, languages: list[str] | None, strict: bool = False) -> dict[str, Any]:
        """Fetch one transcript as plain snippet dicts, plus what describes it.

        The cache is keyed on the resolved track, not on the requested languages:
        `lang=fr,en` and `lang=en` landing on the same English track share one entry,
        and a page request that forgets `lang` still hits it once the track list is
        known. Only plain data is cached: library objects hold the requests session.
        """
        wanted = languages or self.settings.default_languages()
        listed = self._cached(("languages", video_id))
        if listed is not None:
            tracks = listed["languages"]
            index, fallback = pick_track(tracks, wanted, strict)
            if index is None:
                raise language_miss(tracks, wanted)
            cached = self._cached(self._transcript_key(video_id, tracks[index]))
            if cached is not None:
                return {**cached, "fallback": fallback, "available_languages": tracks, "cached": True}

        def work(api: Any, session: requests.Session) -> dict[str, Any]:
            found = list(api.list(video_id))
            tracks = [track_info(track) for track in found]
            self._store(("languages", video_id), {"languages": tracks})
            index, fallback = pick_track(tracks, wanted, strict)
            if index is None:
                raise language_miss(tracks, wanted)
            track = found[index]
            fetched = track.fetch()
            data: dict[str, Any] = {
                "video_id": video_id,
                "url": watch_url(video_id),
                "language_code": track.language_code,
                "language": track.language,
                "is_generated": bool(track.is_generated),
                "snippets": [
                    {"start": float(snippet.start), "duration": float(snippet.duration), "text": snippet.text}
                    for snippet in fetched.snippets
                ],
            }
            details = getattr(api, "details", None) or {}
            if details.get("title"):
                data["title"] = details["title"]
                data["channel"] = details.get("author")
            elif self.settings.yt_fetch_title:
                data.update(self._oembed(session, video_id))
            try:
                data["length_seconds"] = float(details["lengthSeconds"])
            except (KeyError, TypeError, ValueError):
                pass
            logger.info("Transcript %s served via %s path", video_id, getattr(api, "path", None) or "full")
            self._store(self._transcript_key(video_id, tracks[index]), data)
            return {**data, "fallback": fallback, "available_languages": tracks}

        return {**self._with_retries(video_id, work), "cached": False}

    @staticmethod
    def _transcript_key(video_id: str, track: dict[str, Any]) -> tuple[Any, ...]:
        return ("transcript", video_id, track["code"], track["generated"])

    # ---------------------------------------------------------------- upstream

    def _routes(self) -> list[str | None]:
        """Egress for each attempt of one call, in order. None means the server's own IP.

        The server IP is tried at most once: retrying a blocked IP gains nothing,
        while each retry through a rotating residential gateway draws a new exit IP.
        """
        proxies = self.settings.proxy_urls()
        if not proxies:
            return [None]
        with self._lock:
            start = self._proxy_index % len(proxies)
            self._proxy_index += 1
        attempts = max(1, self.settings.yt_max_retries)
        routes: list[str | None] = [proxies[(start + offset) % len(proxies)] for offset in range(attempts)]
        if self.settings.yt_direct_first:
            return [None, *routes]
        if self.settings.yt_allow_direct_fallback:
            return [*routes, None]
        return routes

    def _with_retries(self, video_id: str, work: Callable[[Any, requests.Session], dict[str, Any]]) -> dict[str, Any]:
        """Run `work` on a fresh session per attempt, moving egress on block.

        A fresh YouTubeTranscriptApi per attempt is required, not just convenient: the
        library is not thread-safe, and endpoints run in the anyio thread pool.
        """
        routes = self._routes()
        deadline = time.monotonic() + max(1.0, float(self.settings.yt_deadline))
        last_error: Exception | None = None
        for attempt, route in enumerate(routes, start=1):
            if time.monotonic() >= deadline:
                break
            self._throttle()
            session = TimeoutSession(self.settings.yt_timeout, deadline)
            try:
                return work(self._api_factory(session, route), session)
            except YouTubeError:
                raise
            except RETRYABLE as exc:
                last_error = exc
                logger.warning(
                    "YouTube attempt %d/%d for %s via %s failed: %s",
                    attempt,
                    len(routes),
                    video_id,
                    "proxy" if route else "direct",
                    type(exc).__name__,
                )
            except CouldNotRetrieveTranscript as exc:
                raise map_error(exc) from exc
            finally:
                session.close()
        raise map_error(last_error) from last_error

    def _oembed(self, session: requests.Session, video_id: str) -> dict[str, Any]:
        """Best effort: a failed title lookup must never fail the transcript."""
        try:
            response = session.get(OEMBED_URL, params={"url": watch_url(video_id), "format": "json"})
            if response.status_code != 200:
                return {}
            payload = response.json()
            return {"title": payload.get("title"), "channel": payload.get("author_name")}
        except Exception as exc:  # noqa: BLE001
            logger.info("oEmbed lookup failed for %s: %s", video_id, exc)
            return {}

    def _throttle(self) -> None:
        interval = max(0.0, float(self.settings.yt_min_interval))
        if interval <= 0:
            return
        with self._throttle_lock:
            wait = self._last_request + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    # ------------------------------------------------------------------- cache

    def _cached(self, key: Any) -> Any | None:
        ttl = max(0.0, float(self.settings.yt_cache_ttl))
        if ttl <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            stored_at, data = entry
            if now - stored_at > ttl:
                self._cache.pop(key, None)
                return None
            return data

    def _store(self, key: Any, data: Any) -> None:
        if self.settings.yt_cache_ttl <= 0:
            return
        max_entries = max(1, int(self.settings.yt_cache_max_entries))
        with self._lock:
            self._cache[key] = (time.monotonic(), data)
            while len(self._cache) > max_entries:
                oldest = min(self._cache, key=lambda item: self._cache[item][0])
                self._cache.pop(oldest, None)


def map_error(exc: Exception | None) -> YouTubeError:
    if isinstance(exc, YouTubeError):
        return exc
    if isinstance(exc, InvalidVideoId):
        return YouTubeError(422, "invalid_video_id", "Invalid video ID: pass an 11-character ID or a YouTube URL.")
    if isinstance(exc, VideoUnavailable):
        return YouTubeError(404, "video_unavailable", "Video unavailable (deleted, private or wrong ID).")
    if isinstance(exc, TranscriptsDisabled):
        return YouTubeError(404, "no_transcripts", "Subtitles are disabled or absent for this video.")
    if isinstance(exc, AgeRestricted):
        return YouTubeError(422, "age_restricted", "Age-restricted video: subtitles need a signed-in account.")
    if isinstance(exc, VideoUnplayable):
        reason = getattr(exc, "reason", None) or "unplayable"
        return YouTubeError(422, "video_unplayable", f"Video unplayable: {reason}.")
    if isinstance(exc, PoTokenRequired):
        return YouTubeError(502, "po_token_required", "YouTube requires a PO token for this track: not retrievable for now.")
    if isinstance(exc, RequestBlocked):
        return YouTubeError(502, "blocked", "YouTube blocked every egress IP tried. Retry later.")
    if isinstance(exc, requests.RequestException):
        return YouTubeError(502, "network_error", f"Could not reach YouTube: {type(exc).__name__}.")
    return YouTubeError(502, "upstream_error", "YouTube request failed. Retry later.")
