"""Turn whatever the agent pasted into an 11-character YouTube video ID."""

import re
from urllib.parse import parse_qs, urlparse


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
# /shorts/<id>, /embed/<id>, /live/<id>, /v/<id>, /e/<id>
PATH_PREFIXES = {"shorts", "embed", "live", "v", "e"}


class InvalidVideo(ValueError):
    pass


def parse_video_id(value: str) -> str:
    """Accept a bare ID or any common YouTube URL form, and return the bare ID.

    Raises InvalidVideo before any network call, so a malformed input never costs
    a proxied watch-page download.
    """
    raw = (value or "").strip()
    if not raw:
        raise InvalidVideo("empty video")
    if VIDEO_ID_RE.match(raw):
        return raw

    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    segments = [part for part in parsed.path.split("/") if part]

    found: str | None = None
    if host in SHORT_HOSTS:
        found = segments[0] if segments else None
    elif host in YOUTUBE_HOSTS:
        query = parse_qs(parsed.query)
        if query.get("v"):
            found = query["v"][0]
        elif len(segments) >= 2 and segments[0] in PATH_PREFIXES:
            found = segments[1]
    else:
        raise InvalidVideo(f"not a YouTube URL: {raw[:120]}")

    if found and VIDEO_ID_RE.match(found):
        return found
    raise InvalidVideo(f"no video ID found in: {raw[:120]}")


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"
