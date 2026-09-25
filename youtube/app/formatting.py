"""Shape raw caption snippets into a compact, citable, pageable response."""

import re
from typing import Any


WHITESPACE_RE = re.compile(r"\s+")


def clean_text(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text or "").strip()


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def window(snippets: list[dict[str, Any]], start: float | None, end: float | None) -> list[dict[str, Any]]:
    """Keep the snippets that begin inside [start, end)."""
    return [
        snippet
        for snippet in snippets
        if (start is None or snippet["start"] >= start) and (end is None or snippet["start"] < end)
    ]


def paragraphs(snippets: list[dict[str, Any]], paragraph_seconds: float) -> list[dict[str, Any]]:
    """Merge consecutive snippets into blocks spanning about `paragraph_seconds`.

    Captions arrive as 2-4 second fragments; one timestamp per fragment would double
    the token count for no gain. A block keeps the start of its first fragment so the
    agent can still cite a position.
    """
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for snippet in snippets:
        text = clean_text(snippet["text"])
        if not text:
            continue
        if current is None or snippet["start"] - current["start"] >= paragraph_seconds:
            current = {"start": snippet["start"], "parts": []}
            blocks.append(current)
        current["parts"].append(text)
    return [
        {"start": block["start"], "text": f"[{format_timestamp(block['start'])}] {' '.join(block['parts'])}"}
        for block in blocks
    ]


def paginate(items: list[dict[str, Any]], max_chars: int, key: str = "text") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Cut `items` at the last whole item under `max_chars`.

    Always keeps at least one item so a single oversized block still makes progress.
    Returns the kept items and the paging fields (`truncated`, `next_start`, ...).
    """
    total_chars = sum(len(item[key]) + 1 for item in items)
    kept: list[dict[str, Any]] = []
    used = 0
    for item in items:
        cost = len(item[key]) + 1
        if kept and used + cost > max_chars:
            break
        kept.append(item)
        used += cost
    truncated = len(kept) < len(items)
    paging: dict[str, Any] = {"chars": used, "total_chars": total_chars, "truncated": truncated}
    if truncated:
        # Never rounded: rounding 49.226 up to 49.23 would make the next page's
        # `start >= next_start` window skip the caption at the boundary.
        paging["next_start"] = items[len(kept)]["start"]
    return kept, paging


def render(
    snippets: list[dict[str, Any]],
    *,
    output: str,
    start: float | None,
    end: float | None,
    max_chars: int,
    paragraph_seconds: float,
) -> dict[str, Any]:
    selected = window(snippets, start, end)
    if output == "segments":
        # `start` stays raw for paging; `t` is the rounded value shown to the agent.
        items = [
            {
                "start": snippet["start"],
                "t": round(snippet["start"], 2),
                "d": round(snippet["duration"], 2),
                "text": clean_text(snippet["text"]),
            }
            for snippet in selected
            if clean_text(snippet["text"])
        ]
        kept, paging = paginate(items, max_chars)
        return {"segments": [{k: v for k, v in item.items() if k != "start"} for item in kept], **paging}

    blocks = paragraphs(selected, paragraph_seconds)
    kept, paging = paginate(blocks, max_chars)
    return {"text": "\n".join(block["text"] for block in kept), **paging}


def duration(snippets: list[dict[str, Any]]) -> float | None:
    if not snippets:
        return None
    last = snippets[-1]
    return round(last["start"] + last["duration"], 2)
