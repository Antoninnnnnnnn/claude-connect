"""Tests for the two lbc bugs app/lbc_client.py works around.

`_sanitize_payload` exists because `lbc.utils.build_search_payload_with_url`:

1. never percent-decodes query values, so `text=nike+tn` is searched literally;
2. tests the *value* instead of the key in its `page` guard, turning `&page=2`
   into a bogus `filters.enums.page` that makes the API return zero ads.

The unit tests below pin our repair. `test_upstream_bugs_still_present` is the
canary: if a future lbc release fixes them, it fails and tells us to re-check the
workaround instead of silently double-handling the values.
"""

import pytest

from app.lbc_client import LeboncoinClient

lbc_utils = pytest.importorskip("lbc.utils")


def test_removes_bogus_page_enum():
    payload = {"filters": {"enums": {"page": ["2"], "ad_type": ["offer"]}}}
    LeboncoinClient._sanitize_payload(payload)
    assert "page" not in payload["filters"]["enums"]
    assert payload["filters"]["enums"]["ad_type"] == ["offer"]


def test_percent_decodes_enum_values():
    payload = {"filters": {"enums": {"brand": ["Nike%2BAir", "adidas+originals"]}}}
    LeboncoinClient._sanitize_payload(payload)
    assert payload["filters"]["enums"]["brand"] == ["Nike+Air", "adidas originals"]


def test_percent_decodes_keyword_text():
    payload = {"filters": {"keywords": {"text": "nike+tn+requin"}}}
    LeboncoinClient._sanitize_payload(payload)
    assert payload["filters"]["keywords"]["text"] == "nike tn requin"


def test_decodes_accented_text():
    payload = {"filters": {"keywords": {"text": "v%C3%A9lo+%C3%A9lectrique"}}}
    LeboncoinClient._sanitize_payload(payload)
    assert payload["filters"]["keywords"]["text"] == "vélo électrique"


def test_drops_enums_key_when_it_becomes_empty():
    """An enums dict holding only the bogus page must not survive as `{}`."""
    payload = {"filters": {"enums": {"page": ["3"]}}}
    LeboncoinClient._sanitize_payload(payload)
    assert "enums" not in payload["filters"]


def test_keeps_non_string_enum_values_untouched():
    payload = {"filters": {"enums": {"price": [10, 20]}}}
    LeboncoinClient._sanitize_payload(payload)
    assert payload["filters"]["enums"]["price"] == ["10", "20"]


@pytest.mark.parametrize("payload", [{}, {"filters": None}, {"filters": "nope"}, {"filters": {}}])
def test_tolerates_missing_or_malformed_filters(payload):
    LeboncoinClient._sanitize_payload(payload)  # must not raise


def test_non_dict_keywords_left_alone():
    payload = {"filters": {"keywords": "nike+tn"}}
    LeboncoinClient._sanitize_payload(payload)
    assert payload["filters"]["keywords"] == "nike+tn"


def test_upstream_bugs_still_present():
    """Canary: assert the lbc bugs we compensate for still exist.

    If this fails after a dependency bump, `_sanitize_payload` may now be
    double-decoding or stripping a legitimate key. Re-read it before pinning.
    """
    payload = lbc_utils.build_search_payload_with_url(
        url="https://www.leboncoin.fr/recherche?text=nike+tn&page=2",
        limit=1,
        page=2,
    )
    filters = payload.get("filters") or {}
    keywords = filters.get("keywords") or {}
    enums = filters.get("enums") or {}

    still_broken = keywords.get("text") == "nike+tn" or "page" in enums
    assert still_broken, (
        "lbc no longer mangles text/page: review _sanitize_payload in "
        "app/lbc_client.py before bumping the pin"
    )


def test_sanitize_is_idempotent():
    """Running it twice must not decode already-decoded values a second time."""
    payload = {"filters": {"keywords": {"text": "100%25+coton"}}}
    LeboncoinClient._sanitize_payload(payload)
    once = payload["filters"]["keywords"]["text"]
    LeboncoinClient._sanitize_payload(payload)
    assert payload["filters"]["keywords"]["text"] == once == "100% coton"
