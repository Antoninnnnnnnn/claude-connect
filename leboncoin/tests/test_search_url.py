"""Search URL construction: category, location, price and sort mapping."""

from urllib.parse import parse_qs, urlparse

import pytest

from app.lbc_client import LeboncoinClient, LeboncoinError


def params_of(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


# --------------------------------------------------------------------- category


def test_numeric_category_passes_through(client):
    assert client._category_id("53") == "53"


@pytest.mark.parametrize(
    ("alias", "expected"),
    [("sneakers", "53"), ("chaussures", "53"), ("mode", "72"), ("vêtements", "22"), ("velo", "55")],
)
def test_category_aliases(client, alias, expected):
    assert client._category_id(alias) == expected


def test_category_alias_is_accent_and_case_insensitive(client):
    assert client._category_id("VETEMENTS") == client._category_id("vêtements") == "22"


def test_unknown_category_raises(client):
    with pytest.raises(LeboncoinError, match="Unknown category"):
        client._category_id("category-that-does-not-exist")


def test_none_category_is_none(client):
    assert client._category_id(None) is None


# --------------------------------------------------------------------- location


def test_lat_lng_builds_p_api_location(client):
    value = client._location_value(location=None, lat=48.8566, lng=2.3522, radius=5000)
    assert value == "p_api__48.8566_2.3522_5000_5000"


def test_lat_lng_defaults_radius(client):
    value = client._location_value(location=None, lat=1.0, lng=2.0, radius=None)
    assert value.endswith("_10000_10000")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("dept:75", "d_75"),
        ("departement:75", "d_75"),
        ("département:75", "d_75"),
        ("region:12", "r_12"),
        ("région:12", "r_12"),
        ("75", "d_75"),
        ("d_75", "d_75"),
        ("r_12", "r_12"),
    ],
)
def test_location_shorthands(client, raw, expected):
    assert client._location_value(location=raw, lat=None, lng=None, radius=None) == expected


def test_location_csv_triplet(client):
    value = client._location_value(location="48.8566,2.3522,15000", lat=None, lng=None, radius=None)
    assert value == "p_api__48.8566_2.3522_15000_15000"


def test_location_csv_pair_defaults_radius(client):
    value = client._location_value(location="48.8566,2.3522", lat=None, lng=None, radius=None)
    assert value == "p_api__48.8566_2.3522_10000_10000"


def test_location_by_department_name(client):
    assert client._location_value(location="paris", lat=None, lng=None, radius=None) == "d_75"


def test_prebuilt_location_passes_through(client):
    raw = "p_api__1_2_3_3"
    assert client._location_value(location=raw, lat=None, lng=None, radius=None) == raw


def test_unknown_location_raises(client):
    with pytest.raises(LeboncoinError, match="Unknown location"):
        client._location_value(location="notacity", lat=None, lng=None, radius=None)


# ------------------------------------------------------------------ price range


@pytest.mark.parametrize(
    ("low", "high", "expected"),
    [
        (None, None, None),
        (10, 80, "10-80"),
        (None, 80, "0-80"),
        (10, None, "10-"),
        (10.5, 80.0, "10.5-80"),
    ],
)
def test_range_value(low, high, expected):
    assert LeboncoinClient._range_value(low, high) == expected


# -------------------------------------------------------------------- full URL


def test_build_search_url_maps_everything(client):
    url = client._build_search_url(
        text="nike tn",
        category="sneakers",
        location="dept:75",
        lat=None,
        lng=None,
        radius=None,
        price_min=10,
        price_max=80,
        sort="price_low",
        page=1,
        url=None,
    )
    got = params_of(url)
    assert url.startswith("https://www.leboncoin.fr/recherche?")
    assert got["text"] == "nike tn"
    assert got["category"] == "53"
    assert got["locations"] == "d_75"
    assert got["price"] == "10-80"
    assert got["sort"] == "price" and got["order"] == "asc"
    assert "page" not in got, "page 1 must stay implicit"


def test_build_search_url_adds_page_beyond_first(client):
    url = client._build_search_url(
        text="x", category=None, location=None, lat=None, lng=None, radius=None,
        price_min=None, price_max=None, sort="newest", page=3, url=None,
    )
    assert params_of(url)["page"] == "3"


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("newest", {"sort": "time", "order": "desc"}),
        ("oldest", {"sort": "time", "order": "asc"}),
        ("price_low", {"sort": "price", "order": "asc"}),
        ("price_high", {"sort": "price", "order": "desc"}),
        ("relevance", {"sort": "relevance"}),
    ],
)
def test_sort_mapping(client, sort, expected):
    url = client._build_search_url(
        text="x", category=None, location=None, lat=None, lng=None, radius=None,
        price_min=None, price_max=None, sort=sort, page=1, url=None,
    )
    got = params_of(url)
    for key, value in expected.items():
        assert got[key] == value


def test_unknown_sort_falls_back_to_newest(client):
    url = client._build_search_url(
        text="x", category=None, location=None, lat=None, lng=None, radius=None,
        price_min=None, price_max=None, sort="bogus", page=1, url=None,
    )
    assert params_of(url)["sort"] == "time"


def test_explicit_url_overrides_filters(client):
    url = client._build_search_url(
        text="ignored", category="sneakers", location="dept:75", lat=None, lng=None,
        radius=None, price_min=1, price_max=2, sort="newest", page=2,
        url="https://www.leboncoin.fr/recherche?text=kept",
    )
    got = params_of(url)
    assert got["text"] == "kept"
    assert got["page"] == "2"
    assert "category" not in got


# --------------------------------------------------------------------- paging


def test_with_page_adds_and_strips():
    base = "https://www.leboncoin.fr/recherche?text=nike"
    assert "page=2" in LeboncoinClient._with_page(base, 2)
    assert "page" not in LeboncoinClient._with_page(f"{base}&page=5", 1)


def test_with_page_accepts_relative_url():
    assert LeboncoinClient._with_page("/recherche?text=x", 2).startswith(
        "https://www.leboncoin.fr/recherche"
    )
