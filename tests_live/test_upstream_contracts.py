"""Contract canaries: assert real upstream responses still carry what we promise.

Each test names the upstream change it is meant to catch. A failure here means
the site moved, not that our code regressed: read the assertion, then fix the
parser in the matching service.
"""

import pytest

pytestmark = pytest.mark.live


# --------------------------------------------------------------------- health


@pytest.mark.parametrize("service", ["vinted", "leboncoin", "ecoledirecte", "lacentrale"])
def test_health_is_up(http, bases, service):
    response = http.get(f"{bases[service]}/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


@pytest.mark.parametrize("service", ["vinted", "leboncoin", "ecoledirecte", "lacentrale"])
def test_api_key_is_enforced(http, bases, service):
    path = {"ecoledirecte": "/status"}.get(service, "/search")
    response = http.get(f"{bases[service]}{path}")
    assert response.status_code == 401, "protected endpoint answered without a key"
    assert response.json()["ok"] is False


# --------------------------------------------------------------------- Vinted


def test_vinted_search_still_parses(call):
    """Catches: Vinted renaming catalog item fields."""
    data = call("vinted", "/search", query="nike", per_page=5).json()
    assert data["ok"] is True
    items = data["data"]["items"]
    assert items, "no items: search contract or session handling broke"
    first = items[0]
    for field in ("id", "title", "price", "url"):
        assert first.get(field) is not None, f"Vinted item lost `{field}`"
    assert first["url"].startswith("https://www.vinted.")


def test_vinted_brand_and_size_present_somewhere(call):
    """These come from *_title fields that Vinted has renamed before."""
    items = call("vinted", "/search", query="nike", per_page=20).json()["data"]["items"]
    assert any(item.get("brand") for item in items), "no item carried a brand"
    assert any(item.get("size") for item in items), "no item carried a size"


def test_vinted_price_stats(call):
    data = call("vinted", "/price-stats", query="nike", per_page=30).json()["data"]
    assert data["count"] > 0
    assert data["min"] is not None and data["max"] is not None


# ------------------------------------------------------------------ Leboncoin


def test_leboncoin_search_still_parses(call):
    """Catches: /finder/search renaming list_id, subject or price_cents."""
    data = call(
        "leboncoin", "/search", text="nike", category="sneakers", limit=5
    ).json()
    assert data["ok"] is True
    items = data["data"]["items"]
    assert items, "no ads: payload building or DataDome handling broke"
    first = items[0]
    for field in ("id", "title", "price", "url"):
        assert first.get(field) is not None, f"Leboncoin ad lost `{field}`"
    assert first["url"].startswith("https://www.leboncoin.fr/")


def test_leboncoin_pagination_metadata(call):
    """`total` disappearing would silently break result-count reporting."""
    pagination = call("leboncoin", "/search", text="velo", limit=5).json()["data"]["pagination"]
    assert pagination["returned"] > 0
    assert pagination.get("total") is not None, "search lost its `total`"


def test_leboncoin_text_query_is_actually_applied(call):
    """Regression guard for the lbc percent-decoding bug.

    If `_sanitize_payload` stopped decoding, the API would search the literal
    string `velo+electrique` and return nothing.
    """
    data = call("leboncoin", "/search", text="velo electrique", limit=10).json()["data"]
    assert data["items"], "multi-word search returned nothing: check _sanitize_payload"


def test_leboncoin_paging_returns_different_ads(call):
    """Regression guard for the bogus `filters.enums.page` bug."""
    first = call("leboncoin", "/search", text="nike", limit=5, page=1).json()["data"]["items"]
    second = call("leboncoin", "/search", text="nike", limit=5, page=2).json()["data"]["items"]
    assert second, "page 2 was empty: the page filter may be mangled again"
    assert {item["id"] for item in first} != {item["id"] for item in second}


def test_leboncoin_category_filter_is_honoured(call):
    items = call("leboncoin", "/search", text="nike", category="sneakers", limit=10).json()["data"]["items"]
    assert items
    assert all(item.get("category_id") == "53" for item in items)


# ----------------------------------------------------------------- La Centrale


def test_centrale_search_still_parses(call):
    """Catches: recherche.lacentrale.fr v5 changing its hit shape."""
    data = call(
        "lacentrale", "/search", make="RENAULT", model="ZOE", limit=5
    ).json()
    assert data["ok"] is True
    items = data["data"]["items"]
    assert items, "no hits: upstream key, params or JSON shape changed"
    first = items[0]
    for field in ("id", "title", "make", "price", "url"):
        assert first.get(field) is not None, f"La Centrale hit lost `{field}`"


def test_centrale_listing_url_scheme_still_valid(call):
    """The detail URL encodes the ref letter as its ASCII code.

    If lacentrale.fr reverts that scheme, every link we hand out 404s.
    """
    import httpx

    items = call("lacentrale", "/search", make="RENAULT", model="ZOE", limit=1).json()["data"]["items"]
    url = items[0]["url"]
    with httpx.Client(timeout=45.0, follow_redirects=True) as session:
        response = session.head(url)
    assert response.status_code != 404, f"listing URL scheme broke: {url}"


def test_centrale_vehicle_block_present(call):
    data = call(
        "lacentrale", "/search", make="RENAULT", model="ZOE", limit=3, include_vehicle="true"
    ).json()["data"]
    assert any(item.get("vehicle") for item in data["items"]), "vehicle block vanished"


def test_centrale_ev_metadata_extraction_still_fires(call):
    """ZOE titles carry `22KWH` / `Q210`; losing this means the regexes went stale."""
    items = call("lacentrale", "/search", make="RENAULT", model="ZOE", limit=24).json()["data"]["items"]
    assert any(item.get("battery_capacity_kwh") for item in items), (
        "no ZOE reported a battery capacity: check extract_ev_metadata"
    )


def test_centrale_zip_distance_filter_is_accepted(call):
    """A rejected bucket value would come back as an upstream 4xx."""
    response = call(
        "lacentrale", "/search", make="RENAULT", zip="27000", distance_km=50, limit=3
    )
    assert response.status_code == 200


def test_centrale_price_stats(call):
    data = call("lacentrale", "/price-stats", make="RENAULT", model="ZOE", limit=30).json()["data"]
    assert data["priced_count"] > 0
    assert data["median"] is not None


def test_centrale_listing_detail(call):
    items = call("lacentrale", "/search", make="RENAULT", model="ZOE", limit=1).json()["data"]["items"]
    data = call("lacentrale", f"/listing/{items[0]['id']}").json()
    assert data["ok"] is True
    assert data["data"]["item"]["id"] == items[0]["id"]


def test_centrale_rejects_foreign_url(call):
    """SSRF guard on the `url` passthrough."""
    response = call(
        "lacentrale", "/search", allow_errors=True, url="https://evil.example/listing"
    )
    assert response.status_code in {422, 502}
    assert response.json()["ok"] is False


# ---------------------------------------------------------------- EcoleDirecte


def test_ecoledirecte_login_works(call):
    """Catches: another APIVERSION bump or a new auth header requirement."""
    session = call("ecoledirecte", "/status").json()["data"]["session"]
    if not session["logged_in"]:
        pytest.fail(
            f"not logged in (last error: {session['last_error']}). "
            "An APIVERSION bump usually means upgrading the ecoledirecte package."
        )


def test_ecoledirecte_homework_decodes(call):
    """Regression guard for the missing `base64` import."""
    data = call("ecoledirecte", "/homework").json()
    assert data["ok"] is True
    for entries in data["data"].values():
        for entry in entries:
            assert "subject" in entry
            if "content" in entry:
                assert "Erreur de décodage" not in entry["content"]


def test_ecoledirecte_schedule(call):
    data = call("ecoledirecte", "/schedule").json()
    assert data["ok"] is True
    assert isinstance(data["data"], list)


def test_ecoledirecte_status_does_not_leak_credentials(call, api_key):
    """`last_error` embeds the upstream payload, which contains the login body."""
    import json as json_lib

    body = json_lib.dumps(call("ecoledirecte", "/status").json())
    assert "motdepasse" not in body or "[redacted]" in body


def test_ecoledirecte_health_hides_session_state(http, bases):
    """Public probe must not expose login state; /status is key-protected."""
    data = http.get(f"{bases['ecoledirecte']}/health").json()
    assert "session" not in data["data"]
