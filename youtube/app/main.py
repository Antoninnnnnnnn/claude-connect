import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any, Literal

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.formatting import duration, render
from app.innertube import InvalidReference, decode_cursor, parse_playlist_id
from app.video_id import InvalidVideo, parse_video_id
from app.yt_client import YouTubeClient, YouTubeError


logger = logging.getLogger(__name__)
settings = get_settings()
youtube = YouTubeClient(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.api_key:
        raise RuntimeError("API_KEY is not configured")
    yield


app = FastAPI(
    title="Self-hosted YouTube Transcript API",
    version="1.0.0",
    docs_url="/docs",
    lifespan=lifespan,
)


@app.exception_handler(YouTubeError)
async def youtube_error_handler(_: Request, exc: YouTubeError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"ok": False, "error": str(exc), "error_code": exc.code, **exc.extra},
    )


@app.exception_handler(InvalidVideo)
async def invalid_video_handler(_: Request, exc: InvalidVideo) -> JSONResponse:
    return JSONResponse(status_code=422, content={"ok": False, "error": str(exc), "error_code": "invalid_video_id"})


@app.exception_handler(InvalidReference)
async def invalid_reference_handler(_: Request, exc: InvalidReference) -> JSONResponse:
    return JSONResponse(status_code=422, content={"ok": False, "error": str(exc), "error_code": "invalid_reference"})


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": str(exc.detail)})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"ok": False, "error": str(exc.errors())})


@app.exception_handler(Exception)
async def generic_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"ok": False, "error": "Internal server error"})


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    current_settings: Settings = Depends(get_settings),
) -> None:
    if not current_settings.api_key:
        raise HTTPException(status_code=500, detail="API_KEY is not configured")
    if not x_api_key or not secrets.compare_digest(x_api_key, current_settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")


def split_languages(value: str | None) -> list[str] | None:
    if not value:
        return None
    codes = [part.strip() for part in value.split(",") if part.strip()]
    return codes or None


@app.get("/health")
async def health() -> dict[str, Any]:
    proxy_count = len(settings.proxy_urls())
    return {"ok": True, "data": {"status": "up", "proxy_configured": proxy_count > 0, "proxy_count": proxy_count}}


@app.get("/languages", dependencies=[Depends(require_api_key)])
async def languages(
    video: str = Query(..., description="Video ID or any YouTube URL (watch, youtu.be, shorts, embed, live)."),
) -> dict[str, Any]:
    video_id = parse_video_id(video)
    data = await anyio.to_thread.run_sync(lambda: youtube.languages(video_id))
    return {"ok": True, "data": data}


@app.get("/transcript", dependencies=[Depends(require_api_key)])
async def transcript(
    video: str = Query(..., description="Video ID or any YouTube URL (watch, youtu.be, shorts, embed, live)."),
    lang: str | None = Query(default=None, description="Preferred language codes in order, e.g. fr,en. Default from YT_DEFAULT_LANGUAGES."),
    strict: bool = Query(default=False, description="Fail instead of falling back to another language."),
    format: Literal["text", "segments"] = Query(default="text", description="text: timestamped paragraphs. segments: raw [{t, d, text}]."),
    start: float | None = Query(default=None, ge=0, description="Only captions starting at or after this second. Use next_start to page."),
    end: float | None = Query(default=None, ge=0, description="Only captions starting before this second."),
    max_chars: int | None = Query(default=None, ge=500, le=200000, description="Output cap. Default from YT_DEFAULT_MAX_CHARS."),
    paragraph_seconds: float = Query(default=45, ge=5, le=600, description="Paragraph length for format=text."),
    include_languages: bool = Query(default=False, description="Add the list of available subtitle tracks."),
) -> dict[str, Any]:
    video_id = parse_video_id(video)
    if start is not None and end is not None and end <= start:
        raise HTTPException(status_code=422, detail="end must be greater than start")
    result = await anyio.to_thread.run_sync(
        lambda: youtube.transcript(video_id, languages=split_languages(lang), strict=strict)
    )
    body = render(
        result["snippets"],
        output=format,
        start=start,
        end=end,
        max_chars=max_chars or settings.yt_default_max_chars,
        paragraph_seconds=paragraph_seconds,
    )
    data: dict[str, Any] = {
        key: result[key]
        for key in ("video_id", "url", "title", "channel", "language_code", "language", "is_generated", "fallback")
        if result.get(key) is not None
    }
    data["duration"] = result.get("length_seconds") or duration(result["snippets"])
    if start is not None:
        data["start"] = start
    if end is not None:
        data["end"] = end
    data.update(body)
    if include_languages or result.get("fallback"):
        # On a fallback the agent needs the list to decide whether to retry.
        data["available_languages"] = result["available_languages"]
    data["cached"] = result["cached"]
    return {"ok": True, "data": data}


LIMIT = Query(default=20, ge=1, le=100, description="Items to return. Pages upstream as needed; `next` continues exactly after the last one.")
NEXT = Query(default=None, max_length=4000, description="`next` from the previous response, with the same other parameters.")


def check_cursor(cursor: str | None) -> None:
    """Reject a mangled `next` before any upstream call (channel resolution included)."""
    if cursor:
        decode_cursor(cursor)


@app.get("/search", dependencies=[Depends(require_api_key)])
async def search(
    q: str = Query(..., min_length=1, max_length=200, description="Search terms, as typed in YouTube's search bar."),
    type: Literal["video", "channel", "playlist", "all"] = Query(default="video", description="all: YouTube's mixed results."),
    duration: Literal["short", "medium", "long"] | None = Query(default=None, description="Videos only. short: <4 min, medium: 4-20 min, long: >20 min."),
    upload: Literal["hour", "today", "week", "month", "year"] | None = Query(default=None, description="Videos only: uploaded within this period."),
    sort: Literal["relevance", "views"] = Query(default="relevance"),
    limit: int = LIMIT,
    next: str | None = NEXT,
) -> dict[str, Any]:
    check_cursor(next)
    video_filters = duration or upload
    if video_filters and type not in ("video", "all"):
        raise HTTPException(status_code=422, detail="duration and upload only apply to type=video")
    search_type = None if type == "all" and not video_filters else ("video" if type == "all" else type)
    data = await anyio.to_thread.run_sync(
        lambda: youtube.search(
            q, type=search_type, duration=duration, upload=upload, sort=sort, limit=limit, cursor=next
        )
    )
    return {"ok": True, "data": data}


@app.get("/channel", dependencies=[Depends(require_api_key)])
async def channel(
    channel: str = Query(..., max_length=300, description="@handle, UC... channel ID, or channel URL."),
    tab: Literal["videos", "shorts", "streams", "playlists"] = Query(default="videos", description="streams: past and upcoming lives."),
    sort: Literal["latest", "popular", "oldest"] = Query(default="latest", description="Not available on the playlists tab."),
    limit: int = LIMIT,
    next: str | None = NEXT,
) -> dict[str, Any]:
    check_cursor(next)
    if tab == "playlists" and sort != "latest":
        raise HTTPException(status_code=422, detail="sort is not available on the playlists tab")
    data = await anyio.to_thread.run_sync(
        lambda: youtube.channel(channel, tab=tab, sort=sort, limit=limit, cursor=next)
    )
    return {"ok": True, "data": data}


@app.get("/playlist", dependencies=[Depends(require_api_key)])
async def playlist(
    playlist: str = Query(..., max_length=300, description="Playlist ID (PL...) or any URL with list=."),
    limit: int = LIMIT,
    next: str | None = NEXT,
) -> dict[str, Any]:
    check_cursor(next)
    playlist_id = parse_playlist_id(playlist)
    data = await anyio.to_thread.run_sync(lambda: youtube.playlist(playlist_id, limit=limit, cursor=next))
    return {"ok": True, "data": data}


@app.get("/video", dependencies=[Depends(require_api_key)])
async def video(
    video: str = Query(..., description="Video ID or any YouTube URL (watch, youtu.be, shorts, embed, live)."),
) -> dict[str, Any]:
    video_id = parse_video_id(video)
    data = await anyio.to_thread.run_sync(lambda: youtube.video(video_id))
    return {"ok": True, "data": data}
