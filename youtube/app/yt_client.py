import json
import logging
import threading
import time
from typing import Any, Callable, TypeVar

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

from app import innertube
from app.config import Settings
from app.light import full_api_factory, light_api_factory
from app.video_id import watch_url


logger = logging.getLogger(__name__)

OEMBED_URL = "https://www.youtube.com/oembed"
# youtube.com's own headers: innertube answers a bare python-requests UA differently.
WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Origin": "https://www.youtube.com",
}

T = TypeVar("T")


class InnertubeBlocked(Exception):
    """innertube answered 429/403: this egress IP is throttled or flagged."""


# A new exit IP can fix these: YouTube flagged the IP, or the proxy itself failed.
RETRYABLE = (
    RequestBlocked,
    YouTubeRequestFailed,
    YouTubeDataUnparsable,
    FailedToCreateConsentCookie,
    InnertubeBlocked,
    requests.RequestException,
)


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
        self._browse_cache: dict[Any, tuple[float, Any]] = {}

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

    def search(
        self,
        query: str,
        *,
        type: str | None,
        duration: str | None,
        upload: str | None,
        sort: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query}
        params = innertube.search_params(type=type, duration=duration, upload=upload, sort=sort)
        if params:
            body["params"] = params
        return self._paged("search", lambda: self._innertube("search", body, f"search {query!r}"), limit, cursor)

    def channel(self, channel: str, *, tab: str, sort: str, limit: int, cursor: str | None) -> dict[str, Any]:
        """A channel tab's items, plus the channel header on the first page.

        A non-default sort is a second request: YouTube only exposes it as the
        continuation token of a chip on the tab's first page.
        """
        channel_id = self._resolve_channel(channel)
        info: dict[str, Any] = {}

        def first() -> dict[str, Any]:
            payload = self._innertube(
                "browse", {"browseId": channel_id, "params": innertube.CHANNEL_TABS[tab]}, f"channel {channel_id}"
            )
            parsed = innertube.parse_channel_info(payload)
            if parsed is None:
                raise YouTubeError(404, "channel_not_found", "This channel does not exist.")
            info.update(parsed)
            if sort == "latest":
                return payload
            chips = innertube.sort_chip_tokens(payload)
            index = innertube.CHANNEL_SORTS[sort]
            if index >= len(chips):
                raise YouTubeError(422, "sort_unavailable", f"This tab offers no '{sort}' sort.")
            return self._innertube("browse", {"continuation": chips[index]}, f"channel {channel_id} {sort}")

        data = self._paged("browse", first, limit, cursor)
        for item in data["items"]:
            # A channel's own page leaves out who uploaded: it is the channel.
            if item["type"] in ("video", "short") and "channel_id" not in item:
                item["channel_id"] = channel_id
        if cursor is None:
            data = {"channel": info, **data}
        return data

    def playlist(self, playlist_id: str, *, limit: int, cursor: str | None) -> dict[str, Any]:
        info: dict[str, Any] = {}

        def first() -> dict[str, Any]:
            try:
                payload = self._innertube("browse", {"browseId": f"VL{playlist_id}"}, f"playlist {playlist_id}")
            except YouTubeError as exc:
                # YouTube answers an unknown playlist ID with a bare HTTP 400.
                if exc.code in ("upstream_rejected", "not_found"):
                    raise YouTubeError(404, "playlist_not_found", "This playlist does not exist or is private.") from exc
                raise
            parsed = innertube.parse_playlist_info(payload, playlist_id)
            if parsed is None:
                raise YouTubeError(404, "playlist_not_found", "This playlist does not exist or is private.")
            info.update(parsed)
            return payload

        data = self._paged("browse", first, limit, cursor)
        if cursor is None:
            data = {"playlist": info, **data}
        return data

    def video(self, video_id: str) -> dict[str, Any]:
        payload = self._innertube(
            "player",
            {"videoId": video_id},
            f"video {video_id}",
            headers={"X-Goog-FieldMask": innertube.VIDEO_FIELD_MASK},
        )
        data = innertube.parse_video(payload)
        if data is None:
            raise YouTubeError(404, "video_unavailable", "Video unavailable (deleted, private or wrong ID).")
        return data

    def _resolve_channel(self, channel: str) -> str:
        kind, value = innertube.parse_channel_ref(channel)
        if kind == "id":
            return value
        try:
            payload = self._innertube("navigation/resolve_url", {"url": value}, f"resolve {value}")
        except YouTubeError as exc:
            if exc.code in ("not_found", "upstream_rejected"):
                raise YouTubeError(404, "channel_not_found", f"No channel at {value}.") from exc
            raise
        browse_id = (payload.get("endpoint") or {}).get("browseEndpoint", {}).get("browseId")
        if not isinstance(browse_id, str) or not browse_id.startswith("UC"):
            raise YouTubeError(404, "channel_not_found", f"No channel at {value}.")
        return browse_id

    def _paged(
        self,
        endpoint: str,
        first: Callable[[], dict[str, Any]],
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        """Collect at least `limit` items across upstream pages, then cut exactly.

        The cursor names the upstream page and how many of its items were served, so a
        cut in the middle of a page loses nothing: the next call re-reads that page
        (a cache hit) and skips ahead. The first page is `first()`, rebuilt from the
        caller's own parameters, which is why they must repeat them with `next`.
        """
        token, offset = innertube.decode_cursor(cursor) if cursor else (None, 0)
        items: list[dict[str, Any]] = []
        next_cursor: str | None = None
        for _ in range(max(1, self.settings.yt_max_pages)):
            payload = self._innertube(endpoint, {"continuation": token}, "page") if token else first()
            page, page_next = innertube.parse_items(payload)
            remaining = page[offset:]
            room = limit - len(items)
            if len(remaining) > room:
                items.extend(remaining[:room])
                next_cursor = innertube.encode_cursor(token, offset + room)
                break
            items.extend(remaining)
            if not page_next:
                next_cursor = None
                break
            token, offset = page_next, 0
            next_cursor = innertube.encode_cursor(token, 0)
            if len(items) >= limit:
                break
        return {"items": items, "count": len(items), "next": next_cursor}

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
        """Transcript attempts: a fresh YouTubeTranscriptApi per attempt is required, not
        just convenient: the library is not thread-safe, and endpoints run in the anyio
        thread pool."""
        return self._attempts(video_id, lambda session, route: work(self._api_factory(session, route), session))

    def _attempts(self, label: str, work: Callable[[requests.Session, str | None], T]) -> T:
        """Run `work` on a fresh session per attempt, moving egress on block."""
        routes = self._routes()
        deadline = time.monotonic() + max(1.0, float(self.settings.yt_deadline))
        last_error: Exception | None = None
        for attempt, route in enumerate(routes, start=1):
            if time.monotonic() >= deadline:
                break
            self._throttle()
            session = TimeoutSession(self.settings.yt_timeout, deadline)
            try:
                return work(session, route)
            except YouTubeError:
                raise
            except RETRYABLE as exc:
                last_error = exc
                logger.warning(
                    "YouTube attempt %d/%d for %s via %s failed: %s",
                    attempt,
                    len(routes),
                    label,
                    "proxy" if route else "direct",
                    type(exc).__name__,
                )
            except CouldNotRetrieveTranscript as exc:
                raise map_error(exc) from exc
            finally:
                session.close()
        raise map_error(last_error) from last_error

    def _innertube(
        self,
        endpoint: str,
        body: dict[str, Any],
        label: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST one innertube WEB call, cached briefly on its exact body."""
        key = ("innertube", endpoint, json.dumps(body, sort_keys=True), json.dumps(headers or {}, sort_keys=True))
        cached = self._cached(key, browse=True)
        if cached is not None:
            return cached
        context = innertube.web_context(self.settings.yt_web_client_version, self.settings.yt_hl, self.settings.yt_gl)

        def work(session: requests.Session, route: str | None) -> dict[str, Any]:
            if route:
                session.proxies = {"http": route, "https": route}
            response = session.post(
                f"{innertube.INNERTUBE_URL}/{endpoint}",
                params={"prettyPrint": "false"},
                json={"context": context, **body},
                headers={**WEB_HEADERS, "Accept-Language": f"{self.settings.yt_hl},en;q=0.8", **(headers or {})},
            )
            status = response.status_code
            if status in (403, 429):
                raise InnertubeBlocked(f"HTTP {status}")
            if status >= 500:
                raise requests.HTTPError(f"{status} Server Error")
            if status == 404:
                raise YouTubeError(404, "not_found", "Not found on YouTube.")
            if status >= 400:
                # Same answer from any IP: a bad argument, or a client version YouTube dropped.
                raise YouTubeError(
                    502,
                    "upstream_rejected",
                    f"YouTube rejected the request (HTTP {status}). If every call fails this way, bump YT_WEB_CLIENT_VERSION.",
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise requests.HTTPError("innertube answer is not JSON") from exc
            if not isinstance(payload, dict):
                raise requests.HTTPError("innertube answer is not an object")
            return payload

        payload = self._attempts(label, work)
        self._store(key, payload, browse=True)
        return payload

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

    def _cache_for(self, browse: bool) -> tuple[dict[Any, tuple[float, Any]], float, int]:
        if browse:
            return self._browse_cache, float(self.settings.yt_browse_cache_ttl), int(self.settings.yt_browse_cache_max_entries)
        return self._cache, float(self.settings.yt_cache_ttl), int(self.settings.yt_cache_max_entries)

    def _cached(self, key: Any, browse: bool = False) -> Any | None:
        cache, ttl, _ = self._cache_for(browse)
        if ttl <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            entry = cache.get(key)
            if not entry:
                return None
            stored_at, data = entry
            if now - stored_at > ttl:
                cache.pop(key, None)
                return None
            return data

    def _store(self, key: Any, data: Any, browse: bool = False) -> None:
        cache, ttl, max_entries = self._cache_for(browse)
        if ttl <= 0:
            return
        max_entries = max(1, max_entries)
        with self._lock:
            cache[key] = (time.monotonic(), data)
            while len(cache) > max_entries:
                oldest = min(cache, key=lambda item: cache[item][0])
                cache.pop(oldest, None)


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
    if isinstance(exc, (RequestBlocked, InnertubeBlocked)):
        return YouTubeError(502, "blocked", "YouTube blocked every egress IP tried. Retry later.")
    if isinstance(exc, requests.RequestException):
        return YouTubeError(502, "network_error", f"Could not reach YouTube: {type(exc).__name__}.")
    return YouTubeError(502, "upstream_error", "YouTube request failed. Retry later.")
