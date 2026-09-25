from app.formatting import clean_text, duration, format_timestamp, paginate, paragraphs, render, window


def snip(start, text, dur=2.0):
    return {"start": start, "duration": dur, "text": text}


def test_format_timestamp():
    assert format_timestamp(0) == "0:00"
    assert format_timestamp(65.9) == "1:05"
    assert format_timestamp(3725) == "1:02:05"
    assert format_timestamp(-3) == "0:00"


def test_clean_text_collapses_newlines():
    assert clean_text("  a\nb\t c  ") == "a b c"


def test_window_bounds():
    items = [snip(0, "a"), snip(10, "b"), snip(20, "c")]
    assert [s["text"] for s in window(items, 10, 20)] == ["b"]
    assert [s["text"] for s in window(items, None, None)] == ["a", "b", "c"]


def test_paragraphs_merge_by_time_and_skip_blank():
    items = [snip(0, "a"), snip(10, "b\nc"), snip(30, "  "), snip(31, "d"), snip(65, "e")]
    blocks = paragraphs(items, 30)
    assert [b["text"] for b in blocks] == ["[0:00] a b c", "[0:31] d", "[1:05] e"]


def test_paginate_cuts_on_whole_item_and_reports_next_start():
    items = [{"start": 0, "text": "x" * 10}, {"start": 40, "text": "y" * 10}, {"start": 80, "text": "z" * 10}]
    kept, paging = paginate(items, 25)
    assert len(kept) == 2
    assert paging == {"chars": 22, "total_chars": 33, "truncated": True, "next_start": 80}


def test_paginate_keeps_oversized_first_item():
    kept, paging = paginate([{"start": 0, "text": "x" * 100}], 10)
    assert len(kept) == 1 and paging["truncated"] is False


def test_render_text_and_segments():
    items = [snip(0, "hello"), snip(50, "world"), snip(100, "  ")]
    text = render(items, output="text", start=None, end=None, max_chars=1000, paragraph_seconds=45)
    assert text["text"] == "[0:00] hello\n[0:50] world"
    assert text["truncated"] is False
    seg = render(items, output="segments", start=None, end=None, max_chars=1000, paragraph_seconds=45)
    assert seg["segments"] == [{"t": 0, "d": 2.0, "text": "hello"}, {"t": 50, "d": 2.0, "text": "world"}]


def test_render_paging_continues_from_next_start():
    items = [snip(i * 10, f"part{i}") for i in range(20)]
    first = render(items, output="text", start=None, end=None, max_chars=40, paragraph_seconds=5)
    assert first["truncated"] is True
    second = render(items, output="text", start=first["next_start"], end=None, max_chars=40, paragraph_seconds=5)
    assert second["text"].startswith(f"[{first['next_start'] // 60:.0f}:")
    assert "part0" not in second["text"]


def test_duration():
    assert duration([]) is None
    assert duration([snip(0, "a"), snip(10, "b", 3.5)]) == 13.5


def test_paging_never_drops_a_boundary_snippet():
    """Auto-generated tracks carry 3-decimal starts: rounding next_start up skipped one."""
    items = [snip(0.0, "a" * 30), snip(49.226, "b" * 30), snip(98.4, "c" * 30)]
    for output, key in (("text", "text"), ("segments", "segments")):
        seen: list[str] = []
        start = None
        for _ in range(5):
            page = render(items, output=output, start=start, end=None, max_chars=40, paragraph_seconds=5)
            seen.append(page[key] if output == "text" else " ".join(s["text"] for s in page[key]))
            if not page["truncated"]:
                break
            start = page["next_start"]
        joined = " ".join(seen)
        for letter in "abc":
            assert letter * 30 in joined, f"{output}: lost the snippet {letter!r}"
