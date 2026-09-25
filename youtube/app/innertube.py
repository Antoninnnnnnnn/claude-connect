"""Search, channel, playlist and video metadata through the innertube WEB API.

These are the JSON endpoints youtube.com itself calls (/youtubei/v1/search, /browse,
/navigation/resolve_url, /player). They answer without an API key, but the payloads
are YouTube's page layout, not a data API: renderers get renamed or migrated
(videoRenderer -> lockupViewModel is half done in 2026-09) without notice. So every
parser here is defensive: an unknown renderer is skipped, a missing field is omitted,
and a shape change shows up as fewer fields or items, never as a crash. The live
canaries in tests_live are what catch it.

Display strings (views, "3 years ago", "12:34") are returned as YouTube renders them:
parsing them to numbers would break on every locale and wording change. /video gives
the exact numbers and date.
"""

import base64
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.video_id import watch_url


INNERTUBE_URL = "https://www.youtube.com/youtubei/v1"
# The player call for /video: 3-5 KB instead of the ~100 KB full answer.
VIDEO_FIELD_MASK = (
    "playabilityStatus(status,reason),"
    "videoDetails(videoId,title,author,channelId,lengthSeconds,viewCount,shortDescription,keywords,isLiveContent),"
    "microformat(playerMicroformatRenderer(publishDate,uploadDate,category))"
)

# Channel tab -> browse params, as sent by youtube.com's tab bar.
CHANNEL_TABS = {
    "videos": "EgZ2aWRlb3PyBgQKAjoA",
    "shorts": "EgZzaG9ydHPyBgUKA5oBAA%3D%3D",
    "streams": "EgdzdHJlYW1z8gYECgJ6AA%3D%3D",
    "playlists": "EglwbGF5bGlzdHPyBgoKCEIGCgIQaCIA",
}
# Sort chips of a channel tab, in the order YouTube shows them (latest, popular, oldest).
# Matched by position: the chip labels are localized.
CHANNEL_SORTS = {"latest": 0, "popular": 1, "oldest": 2}

# Search filter values of the `sp` protobuf (SearchParams{sort=1, filters=2{upload=1, type=2, duration=3}}).
# Sort by upload date (2) is not offered: YouTube ignores it since 2025. Filter on `upload` instead.
SEARCH_TYPES = {"video": 1, "channel": 2, "playlist": 3}
SEARCH_DURATIONS = {"short": 1, "long": 2, "medium": 3}
SEARCH_UPLOADS = {"hour": 1, "today": 2, "week": 3, "month": 4, "year": 5}
SEARCH_SORTS = {"relevance": 0, "views": 3}

CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
HANDLE_RE = re.compile(r"^@?[A-Za-z0-9._-]{3,100}$")


class InvalidReference(ValueError):
    """The channel or playlist argument is not something YouTube can resolve."""


# ------------------------------------------------------------------ requests


def web_context(client_version: str, hl: str, gl: str) -> dict[str, Any]:
    return {"client": {"clientName": "WEB", "clientVersion": client_version, "hl": hl, "gl": gl}}


def _varint(value: int) -> bytes:
    out = b""
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out += bytes([byte | 0x80])
        else:
            return out + bytes([byte])


def search_params(
    type: str | None = None,
    duration: str | None = None,
    upload: str | None = None,
    sort: str | None = None,
) -> str | None:
    """Encode the search filters as the base64 `sp` protobuf youtube.com sends."""
    filters = b""
    if upload:
        filters += b"\x08" + _varint(SEARCH_UPLOADS[upload])
    if type:
        filters += b"\x10" + _varint(SEARCH_TYPES[type])
    if duration:
        filters += b"\x18" + _varint(SEARCH_DURATIONS[duration])
    message = b""
    if sort and SEARCH_SORTS[sort]:
        message += b"\x08" + _varint(SEARCH_SORTS[sort])
    if filters:
        message += b"\x12" + _varint(len(filters)) + filters
    return base64.b64encode(message).decode() if message else None


def parse_channel_ref(value: str) -> tuple[str, str]:
    """Return ("id", "UC...") when the browse id is known, else ("url", url) to resolve."""
    raw = (value or "").strip()
    if CHANNEL_ID_RE.match(raw):
        return "id", raw
    if raw.startswith("@") and HANDLE_RE.match(raw):
        return "url", f"https://www.youtube.com/{raw}"
    if "/" in raw or "." in raw:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = (parsed.hostname or "").lower()
        if host == "youtube.com" or host.endswith(".youtube.com"):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2 and parts[0] == "channel" and CHANNEL_ID_RE.match(parts[1]):
                return "id", parts[1]
            if parts and (parts[0].startswith("@") or (parts[0] in ("c", "user") and len(parts) >= 2)):
                path = parts[0] if parts[0].startswith("@") else f"{parts[0]}/{parts[1]}"
                return "url", f"https://www.youtube.com/{path}"
        raise InvalidReference("Not a YouTube channel: pass @handle, a UC... channel ID or a channel URL.")
    if HANDLE_RE.match(raw):
        return "url", f"https://www.youtube.com/@{raw}"
    raise InvalidReference("Not a YouTube channel: pass @handle, a UC... channel ID or a channel URL.")


def parse_playlist_id(value: str) -> str:
    raw = (value or "").strip()
    if "://" in raw or raw.startswith(("www.", "youtube.com", "m.youtube.com", "music.youtube.com")):
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        listed = parse_qs(parsed.query).get("list")
        raw = listed[0] if listed else ""
    if PLAYLIST_ID_RE.match(raw):
        return raw
    raise InvalidReference("Not a YouTube playlist: pass a playlist ID (PL...) or a URL with list=.")


# -------------------------------------------------------------------- cursor


def encode_cursor(token: str | None, offset: int) -> str:
    """Our `next`: an innertube page token plus how many of its items were already served.

    Lets `limit` cut a page anywhere without losing the rest of it: the next call
    re-reads the same page (from cache) and skips what was served.
    """
    raw = json.dumps({"t": token, "o": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str | None, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        token, offset = data.get("t"), int(data.get("o", 0))
        if (token is not None and not isinstance(token, str)) or offset < 0:
            raise ValueError
        return token, offset
    except (ValueError, TypeError, AttributeError) as exc:
        raise InvalidReference("Invalid `next`: pass the value from the previous response unchanged.") from exc


# ------------------------------------------------------------------- parsing


def find_all(node: Any, key: str) -> list[Any]:
    found: list[Any] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for name, value in current.items():
                if name == key:
                    found.append(value)
                stack.append(value)
        elif isinstance(current, list):
            stack.extend(reversed(current))
    return found


def text(node: Any) -> str | None:
    """Text of any YouTube text shape: {simpleText}, {runs: [...]}, {content}, or a str."""
    if isinstance(node, str):
        return node.strip() or None
    if not isinstance(node, dict):
        return None
    if isinstance(node.get("simpleText"), str):
        return node["simpleText"].strip() or None
    if isinstance(node.get("content"), str):
        return node["content"].strip() or None
    runs = node.get("runs")
    if isinstance(runs, list):
        joined = "".join(run.get("text", "") for run in runs if isinstance(run, dict)).strip()
        return joined or None
    return None


def _clean(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if value not in (None, "", [])}


def _first_channel_id(node: Any) -> str | None:
    for browse_id in find_all(node, "browseId"):
        if isinstance(browse_id, str) and CHANNEL_ID_RE.match(browse_id):
            return browse_id
    return None


def _video_renderer(node: dict[str, Any]) -> dict[str, Any] | None:
    video_id = node.get("videoId")
    if not isinstance(video_id, str):
        return None
    owner = node.get("ownerText") or node.get("longBylineText") or node.get("shortBylineText")
    return _clean(
        {
            "type": "video",
            "id": video_id,
            "title": text(node.get("title")),
            "channel": text(owner),
            "channel_id": _first_channel_id(owner),
            "duration": text(node.get("lengthText")),
            "views": text(node.get("viewCountText")),
            "published": text(node.get("publishedTimeText")),
            "url": watch_url(video_id),
        }
    )


def _metadata_rows(node: Any) -> list[list[str]]:
    rows: list[list[str]] = []
    for row_list in find_all(node, "metadataRows")[:1]:
        for row in row_list if isinstance(row_list, list) else []:
            parts = [text(part.get("text")) for part in row.get("metadataParts", []) if isinstance(part, dict)]
            parts = [part for part in parts if part]
            if parts:
                rows.append(parts)
    return rows


def _lockup(node: dict[str, Any]) -> dict[str, Any] | None:
    content_id = node.get("contentId")
    kind = node.get("contentType")
    if not isinstance(content_id, str):
        return None
    if kind is None:
        # Seen in search, 2026-09: playlist lockups sent without contentType. Video IDs
        # are always 11 characters, playlist IDs longer.
        kind = "LOCKUP_CONTENT_TYPE_VIDEO" if len(content_id) == 11 else "LOCKUP_CONTENT_TYPE_PLAYLIST"
    metadata = node.get("metadata", {}).get("lockupMetadataViewModel", {})
    title = text(metadata.get("title"))
    rows = _metadata_rows(metadata)
    badges = [
        text(badge.get("text"))
        for badge in find_all(node.get("contentImage", {}), "thumbnailBadgeViewModel")
        if isinstance(badge, dict)
    ]
    badge = next((value for value in badges if value), None)
    if kind == "LOCKUP_CONTENT_TYPE_VIDEO":
        # Rows: [channel]?, [views, published]. A channel's own page drops the channel row.
        stats = rows[-1] if rows else []
        return _clean(
            {
                "type": "video",
                "id": content_id,
                "title": title,
                "channel": rows[0][0] if len(rows) >= 2 else None,
                "channel_id": _first_channel_id(metadata),
                "duration": badge,
                "views": stats[0] if stats else None,
                "published": stats[1] if len(stats) >= 2 else None,
                "url": watch_url(content_id),
            }
        )
    if kind in ("LOCKUP_CONTENT_TYPE_PLAYLIST", "LOCKUP_CONTENT_TYPE_PODCAST"):
        return _clean(
            {
                "type": "playlist",
                "id": content_id,
                "title": title,
                "channel": rows[0][0] if rows else None,
                "channel_id": _first_channel_id(metadata),
                "video_count": badge,
                "url": f"https://www.youtube.com/playlist?list={content_id}",
            }
        )
    return None


def _short(node: dict[str, Any]) -> dict[str, Any] | None:
    video_id = next((value for value in find_all(node.get("onTap", {}), "videoId") if isinstance(value, str)), None)
    entity = node.get("entityId")
    if not video_id and isinstance(entity, str) and entity.startswith("shorts-shelf-item-"):
        video_id = entity.removeprefix("shorts-shelf-item-")
    if not video_id:
        return None
    overlay = node.get("overlayMetadata", {})
    return _clean(
        {
            "type": "short",
            "id": video_id,
            "title": text(overlay.get("primaryText")),
            "views": text(overlay.get("secondaryText")),
            "url": f"https://www.youtube.com/shorts/{video_id}",
        }
    )


def _channel_renderer(node: dict[str, Any]) -> dict[str, Any] | None:
    channel_id = node.get("channelId")
    if not isinstance(channel_id, str):
        return None
    # YouTube moved the @handle into subscriberCountText and the subscriber count into
    # videoCountText; read both by content, not by name.
    counts = [text(node.get("subscriberCountText")), text(node.get("videoCountText"))]
    handle = next((value for value in counts if value and value.startswith("@")), None)
    subscribers = next((value for value in counts if value and not value.startswith("@")), None)
    return _clean(
        {
            "type": "channel",
            "id": channel_id,
            "title": text(node.get("title")),
            "handle": handle,
            "subscribers": subscribers,
            "description": text(node.get("descriptionSnippet")),
            "url": f"https://www.youtube.com/{handle}" if handle else f"https://www.youtube.com/channel/{channel_id}",
        }
    )


ITEM_PARSERS = {
    "videoRenderer": _video_renderer,
    "lockupViewModel": _lockup,
    "shortsLockupViewModel": _short,
    "channelRenderer": _channel_renderer,
}
# Lists whose children are results. Anything else (shelves, ads, chip bars, "people
# also watched") is skipped: it would mix unrelated videos into the results.
CONTAINERS = {
    "sectionListRenderer": "contents",
    "itemSectionRenderer": "contents",
    "richGridRenderer": "contents",
    "playlistVideoListRenderer": "contents",
    "gridRenderer": "items",  # a channel's playlists tab
}


def _primary_nodes(payload: dict[str, Any]) -> list[Any]:
    nodes: list[Any] = []
    # Continuation pages: appended items (next page) or reloaded ones (a sort chip).
    for action in [*payload.get("onResponseReceivedCommands", []), *payload.get("onResponseReceivedActions", [])]:
        if not isinstance(action, dict):
            continue
        for key in ("appendContinuationItemsAction", "reloadContinuationItemsCommand"):
            items = action.get(key, {}).get("continuationItems")
            if isinstance(items, list):
                nodes.extend(items)
    contents = payload.get("contents", {})
    search = contents.get("twoColumnSearchResultsRenderer", {}).get("primaryContents")
    if isinstance(search, dict):
        nodes.append(search)
    for tab in contents.get("twoColumnBrowseResultsRenderer", {}).get("tabs", []):
        # Only the selected tab carries `content`.
        content = (tab.get("tabRenderer") or {}).get("content") if isinstance(tab, dict) else None
        if isinstance(content, dict):
            nodes.append(content)
    return nodes


def parse_items(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """Results of one search or browse page, in order, plus the next page token.

    The next token is the last continuationItemRenderer of the result list itself;
    shelves and chip bars carry their own continuations, which are not the next page.
    """
    items: list[dict[str, Any]] = []
    tokens: list[str] = []
    seen: set[tuple[str, str]] = set()

    def walk(nodes: list[Any]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for key, value in node.items():
                if not isinstance(value, dict):
                    continue
                if key == "continuationItemRenderer":
                    token = next(
                        (t for t in find_all(value, "token") if isinstance(t, str)),
                        None,
                    )
                    if token:
                        tokens.append(token)
                elif key == "richItemRenderer":
                    walk([value.get("content", {})])
                elif key in CONTAINERS:
                    walk(value.get(CONTAINERS[key], []))
                elif key in ITEM_PARSERS:
                    item = ITEM_PARSERS[key](value)
                    if item and (item["type"], item["id"]) not in seen:
                        seen.add((item["type"], item["id"]))
                        items.append(item)

    walk(_primary_nodes(payload))
    return items, (tokens[-1] if tokens else None)


def sort_chip_tokens(payload: dict[str, Any]) -> list[str]:
    """Continuation tokens of the channel tab's sort chips, in display order."""
    tokens: list[str] = []
    for chip in find_all(payload, "chipViewModel"):
        token = next((t for t in find_all(chip, "token") if isinstance(t, str)), None)
        if token:
            tokens.append(token)
    return tokens


def parse_channel_info(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Channel header of a first browse page, or None when YouTube says it does not exist."""
    meta = payload.get("metadata", {}).get("channelMetadataRenderer")
    if not isinstance(meta, dict) or not meta.get("externalId"):
        return None
    parts = [part for row in _metadata_rows(payload.get("header", {})) for part in row]
    handle = next((part for part in parts if part.startswith("@")), None)
    counts = [part for part in parts if not part.startswith("@")]
    channel_id = meta["externalId"]
    return _clean(
        {
            "id": channel_id,
            "title": meta.get("title"),
            "handle": handle,
            "subscribers": counts[0] if counts else None,
            "video_count": counts[1] if len(counts) >= 2 else None,
            "description": (meta.get("description") or "").strip() or None,
            "url": f"https://www.youtube.com/{handle}" if handle else f"https://www.youtube.com/channel/{channel_id}",
        }
    )


def parse_playlist_info(payload: dict[str, Any], playlist_id: str) -> dict[str, Any] | None:
    meta = payload.get("metadata", {}).get("playlistMetadataRenderer")
    if not isinstance(meta, dict):
        return None
    return _clean(
        {
            "id": playlist_id,
            "title": meta.get("title"),
            "description": (meta.get("description") or "").strip() or None,
            "channel_id": _first_channel_id(payload.get("header", {})),
            "url": f"https://www.youtube.com/playlist?list={playlist_id}",
        }
    )


def parse_video(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Metadata from the WEB player answer. None when the video does not exist.

    The WEB client often reports UNPLAYABLE (it wants a PO token to stream), yet still
    returns videoDetails and microformat: that is all this needs.
    """
    details = payload.get("videoDetails")
    status = (payload.get("playabilityStatus") or {}).get("status")
    if not isinstance(details, dict) or not details.get("videoId") or status == "ERROR":
        return None
    micro = (payload.get("microformat") or {}).get("playerMicroformatRenderer") or {}

    def as_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    video_id = details["videoId"]
    return _clean(
        {
            "id": video_id,
            "title": details.get("title"),
            "channel": details.get("author"),
            "channel_id": details.get("channelId"),
            "duration_seconds": as_int(details.get("lengthSeconds")),
            "views": as_int(details.get("viewCount")),
            "published": micro.get("publishDate") or micro.get("uploadDate"),
            "category": micro.get("category"),
            "is_live": True if details.get("isLiveContent") else None,
            "keywords": (details.get("keywords") or [])[:20],
            "description": (details.get("shortDescription") or "").strip() or None,
            "url": watch_url(video_id),
        }
    )
