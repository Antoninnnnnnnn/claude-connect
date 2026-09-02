"""Search parameter building, domain resolution and item normalisation."""

import pytest

from app.vinted_client import VintedClient, VintedError


# --------------------------------------------------------------------- domains


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("fr", "https://www.vinted.fr"),
        ("de", "https://www.vinted.de"),
        ("it", "https://www.vinted.it"),
        ("es", "https://www.vinted.es"),
        ("FR", "https://www.vinted.fr"),
        ("  fr  ", "https://www.vinted.fr"),
        ("www.vinted.fr", "https://www.vinted.fr"),
        ("https://www.vinted.de", "https://www.vinted.de"),
    ],
)
def test_base_url_resolution(client, domain, expected):
    assert client._base_url(domain) == expected


def test_base_url_defaults_to_configured_domain(client):
    assert client._base_url(None) == "https://www.vinted.fr"


@pytest.mark.parametrize("domain", ["us", "vinted.co.uk", "evil.example"])
def test_unsupported_domain_rejected(client, domain):
    with pytest.raises(VintedError, match="Unsupported Vinted domain"):
        client._base_url(domain)


# ------------------------------------------------------------------ conditions


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ("new", "6"),
        ("new_with_tags", "6"),
        ("new_without_tags", "1"),
        ("very_good", "2"),
        ("good", "3"),
        ("satisfactory", "4"),
        ("Very Good", "2"),
        ("very-good", "2"),
        ("2", "2"),
        (None, None),
        ("unknown_state", None),
    ],
)
def test_condition_to_id(condition, expected):
    assert VintedClient._condition_to_id(condition) == expected


# ------------------------------------------------------------------- id lists


@pytest.mark.parametrize(
    ("value", "expected"),
    [("53", True), ("53,88", True), ("nike", False), ("53,nike", False), (None, False), ("", False)],
)
def test_is_ids(value, expected):
    assert VintedClient._is_ids(value) is expected


def test_append_ids_expands_csv():
    params: list[tuple[str, str]] = []
    VintedClient._append_ids(params, "brand_ids[]", "53,88")
    assert params == [("brand_ids[]", "53"), ("brand_ids[]", "88")]


def test_append_ids_skips_non_numeric():
    params: list[tuple[str, str]] = []
    VintedClient._append_ids(params, "brand_ids[]", "53, nike ,88")
    assert params == [("brand_ids[]", "53"), ("brand_ids[]", "88")]


def test_append_ids_noop_on_empty():
    params: list[tuple[str, str]] = []
    VintedClient._append_ids(params, "brand_ids[]", None)
    assert params == []


# ----------------------------------------------------------------------- money


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"amount": "45.5", "currency_code": "EUR"}, (45.5, "EUR")),
        ({"value": "10", "currency": "GBP"}, (10.0, "GBP")),
        ({"amount": "45,5", "currency_code": "EUR"}, (45.5, "EUR")),
        (12, (12.0, None)),
        ({"amount": None}, (None, None)),
        ({"amount": "abc"}, (None, None)),
    ],
)
def test_money_parsing(value, expected):
    assert VintedClient._money(value) == expected


# ------------------------------------------------------- item normalisation


def test_normalize_api_item(client):
    item = {
        "id": 42,
        "title": "Nike Jacke",
        "brand_title": "Nike",
        "size_title": "M",
        "status": "Très bon état",
        "price": {"amount": "15.0", "currency_code": "EUR"},
        "total_item_price": {"amount": "16.45", "currency_code": "EUR"},
        "url": "https://www.vinted.fr/items/42-nike-jacke",
        "photo": {"url": "https://images.vinted.net/p.jpg"},
        "user": {"id": 7, "login": "vendeur", "business": False},
    }
    result = client._normalize_item(item)
    assert result.id == 42
    assert result.brand == "Nike" and result.size == "M"
    assert result.price == 15.0 and result.total_item_price == 16.45
    assert result.currency == "EUR"


def test_normalize_gates_photo_and_seller(client):
    item = {"id": 1, "photo": {"url": "https://x/p.jpg"}, "user": {"id": 2, "login": "a"}}
    bare = client._normalize_item(item)
    assert bare.photo is None and bare.seller is None

    rich = client._normalize_item(item, include_photo=True, include_seller=True)
    assert rich.photo == "https://x/p.jpg"
    assert rich.seller == {"id": 2, "login": "a"}


def test_normalize_compacts_seller_nulls(client):
    item = {"id": 1, "user": {"id": 2, "login": "a", "location": None, "rating": None}}
    seller = client._normalize_item(item, include_seller=True).seller
    assert seller == {"id": 2, "login": "a"}


def test_normalize_accepts_seller_key(client):
    item = {"id": 1, "seller": {"id": 9, "login": "b"}}
    assert client._normalize_item(item, include_seller=True).seller["id"] == 9


def test_normalize_raw_opt_in(client):
    item = {"id": 1}
    assert client._normalize_item(item).raw is None
    assert client._normalize_item(item, include_raw=True).raw == item


def test_normalize_empty_item(client):
    result = client._normalize_item({})
    assert result.id is None and result.price is None


# ------------------------------------------------------------------- cookies


def test_cookies_are_not_rewritten_when_unchanged(client, monkeypatch, tmp_path):
    """Regression: the jar was written to disk on every successful request."""
    import httpx

    client._client = httpx.AsyncClient()
    client._client.cookies.set("a", "1", domain="www.vinted.fr")

    writes = []
    original = client.settings.cookie_file.write_text

    def counting_write(*args, **kwargs):
        writes.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(type(client.settings.cookie_file), "write_text", lambda self, *a, **k: writes.append(1))
    monkeypatch.setattr(type(client.settings.cookie_file), "chmod", lambda self, mode: None)

    client._save_cookies()
    client._save_cookies()
    client._save_cookies()
    assert len(writes) == 1, f"{len(writes)} writes for an unchanged jar"


def test_cookie_change_triggers_a_write(client, monkeypatch):
    import httpx

    client._client = httpx.AsyncClient()
    writes = []
    monkeypatch.setattr(type(client.settings.cookie_file), "write_text", lambda self, *a, **k: writes.append(1))
    monkeypatch.setattr(type(client.settings.cookie_file), "chmod", lambda self, mode: None)

    client._client.cookies.set("a", "1", domain="www.vinted.fr")
    client._save_cookies()
    client._client.cookies.set("b", "2", domain="www.vinted.fr")
    client._save_cookies()
    assert len(writes) == 2


# -------------------------------------------------------------------- health


def test_health_status_shape(client):
    status = client.health_status()
    assert status["status"] == "up"
    assert status["started"] is False
    assert "session_refreshes" in status
