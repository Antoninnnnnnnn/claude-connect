"""Vinted item-page scraping.

`_parse_item_page` is the fallback used when the details API refuses a request.
It is pure regex over markup, so every extractor degrades to None on a site
change. Each one is pinned individually to localise breakage.
"""

import pytest

from app.vinted_client import VintedClient

FINAL_URL = "https://www.vinted.fr/items/123456-nike-air-max-90"


# ------------------------------------------------------------ structured data


def test_extracts_ld_json_product(item_page_html):
    data = VintedClient._extract_product_json(item_page_html)
    assert data["name"] == "Nike Air Max 90"
    assert data["offers"]["price"] == 45.0


def test_ld_json_absent_returns_empty():
    assert VintedClient._extract_product_json("<html></html>") == {}


def test_ld_json_ignores_non_product_blocks():
    html = """
    <script type="application/ld+json">{"@type": "BreadcrumbList"}</script>
    <script type="application/ld+json">{"@type": "Product", "name": "Real"}</script>
    """
    assert VintedClient._extract_product_json(html)["name"] == "Real"


def test_ld_json_skips_malformed_block():
    html = """
    <script type="application/ld+json">{not json}</script>
    <script type="application/ld+json">{"@type": "Product", "name": "Real"}</script>
    """
    assert VintedClient._extract_product_json(html)["name"] == "Real"


def test_ld_json_unescapes_entities():
    html = (
        '<script type="application/ld+json">'
        '{"@type": "Product", "name": "Robe &amp; jupe"}</script>'
    )
    assert VintedClient._extract_product_json(html)["name"] == "Robe & jupe"


# --------------------------------------------------------------------- meta


def test_extract_meta_property_and_name(item_page_html):
    assert VintedClient._extract_meta(item_page_html, "og:title") == "Nike Air Max 90 | Vinted"
    assert VintedClient._extract_meta(item_page_html, "description").startswith("Baskets Nike")


def test_extract_meta_missing():
    assert VintedClient._extract_meta("<html></html>", "og:title") is None


# ----------------------------------------------------------------- item id


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.vinted.fr/items/123456-nike", 123456),
        ("/items/999", 999),
        ("https://www.vinted.fr/catalog/1", None),
    ],
)
def test_extract_item_id(url, expected):
    assert VintedClient._extract_item_id(url) == expected


# ------------------------------------------------------------- testid text


def test_extract_testid_text(item_page_html):
    assert VintedClient._extract_testid_text(item_page_html, "item-price") == "45,00 €"


def test_extract_testid_missing():
    assert VintedClient._extract_testid_text("<html></html>", "item-price") is None


def test_extract_item_attribute(item_page_html):
    assert VintedClient._extract_item_attribute(item_page_html, "item-attributes-size", "size") == "42"


def test_extract_item_attribute_missing():
    assert VintedClient._extract_item_attribute("<html></html>", "item-attributes-size", "size") is None


# ----------------------------------------------------------------- price text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("45,00 €", 45.0),
        ("45.00 EUR", 45.0),
        ("1 234,50 €", 1234.50),
        ("48,50 €", 48.5),
        ("gratuit", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_price_text(raw, expected):
    assert VintedClient._parse_price_text(raw) == expected


def test_parse_price_ignores_stray_backslash():
    """Regression: the character class kept backslashes, so float() blew up."""
    assert VintedClient._parse_price_text(r"45\,00 €") == 45.0


# --------------------------------------------------------------------- photos


def test_extract_photos_only_item_images(item_page_html):
    photos = VintedClient._extract_photo_urls(item_page_html)
    assert photos == [
        "https://images.vinted.net/photo1.jpg",
        "https://images.vinted.net/photo2.jpg",
    ]
    assert all("ad.jpg" not in url for url in photos)


def test_extract_photos_dedupes():
    html = '<img class="item-photo-1" src="https://x/a.jpg"><img class="item-photo-2" src="https://x/a.jpg">'
    assert VintedClient._extract_photo_urls(html) == ["https://x/a.jpg"]


def test_extract_photos_none():
    assert VintedClient._extract_photo_urls("<html></html>") == []


# ------------------------------------------------------------------ breadcrumb


def test_extract_breadcrumbs(item_page_html):
    names, ids = VintedClient._extract_breadcrumbs(item_page_html)
    assert names == ["Chaussures", "Hommes"]
    assert ids == [1904, 16]


def test_extract_breadcrumbs_none():
    assert VintedClient._extract_breadcrumbs("<html></html>") == ([], [])


def test_extract_brand_id(item_page_html):
    assert VintedClient._extract_brand_id(item_page_html) == 53


def test_extract_brand_id_missing():
    assert VintedClient._extract_brand_id("<html></html>") is None


# --------------------------------------------------------------------- seller


def test_extract_seller_identity(item_page_html):
    seller = VintedClient._extract_seller(item_page_html)
    assert seller["id"] == 9988776
    assert seller["login"] == "supervendeur"
    assert seller["profile_url"] == "https://www.vinted.fr/member/9988776"
    assert seller["location"] == "Paris, France"
    assert seller["last_seen"] == "il y a 3 heures"


def test_extract_seller_rating(item_page_html):
    """Regression: the rating regex used `\\\\s`, a literal backslash, so it never matched."""
    seller = VintedClient._extract_seller(item_page_html)
    assert seller.get("rating") == 4.8


def test_extract_seller_rating_accepts_comma_decimal():
    html = '<div aria-label="vendeur noté 4,5 sur 5"></div>'
    assert VintedClient._extract_seller(html)["rating"] == 4.5


def test_extract_seller_none():
    assert VintedClient._extract_seller("<html></html>") is None


# ------------------------------------------------------------- full page parse


def test_parse_item_page_maps_everything(client, item_page_html):
    parsed = client._parse_item_page(item_page_html, FINAL_URL)
    assert parsed["id"] == 123456
    assert parsed["title"] == "Nike Air Max 90"
    assert parsed["brand"] == "Nike"
    assert parsed["brand_id"] == 53
    assert parsed["size"] == "42"
    assert parsed["status"] == "Tres bon etat"
    assert parsed["color"] == "Blanc"
    assert parsed["upload_date"] == "il y a 2 jours"
    assert parsed["price"] == {"amount": "45.0", "currency_code": "EUR"}
    assert parsed["total_item_price"] == {"amount": "48.5", "currency_code": "EUR"}
    assert parsed["shipping_price"] == 3.5
    assert parsed["url"] == FINAL_URL
    assert parsed["category"] == "Chaussures > Hommes"
    assert parsed["category_leaf"] == "Hommes"
    assert parsed["category_ids"] == [1904, 16]
    assert parsed["service_fee_included"] is True
    assert parsed["availability"].endswith("InStock")


def test_parse_item_page_strips_vinted_title_suffix(client):
    html = '<meta property="og:title" content="Belle robe | Vinted">'
    assert client._parse_item_page(html, FINAL_URL)["title"] == "Belle robe"


def test_parse_item_page_survives_empty_document(client):
    parsed = client._parse_item_page("<html></html>", FINAL_URL)
    assert parsed["id"] == 123456, "id still recoverable from the URL"
    assert parsed["title"] is None
    assert parsed["price"] is None
    assert parsed["seller"] is None


def test_parse_item_page_price_falls_back_to_testid(client):
    """Without ld+json the visible price must still be read."""
    html = '<div data-testid="item-price">32,00 €</div>'
    parsed = client._parse_item_page(html, FINAL_URL)
    assert parsed["price"]["amount"] == "32.0"


def test_normalize_item_from_parsed_page(client, item_page_html):
    parsed = client._parse_item_page(item_page_html, FINAL_URL)
    result = client._normalize_item(parsed, include_photo=True, include_seller=True)
    assert result.id == 123456
    assert result.price == 45.0
    assert result.currency == "EUR"
    assert result.total_item_price == 48.5
    assert result.seller["login"] == "supervendeur"
    assert result.seller["rating"] == 4.8
    assert result.photo == "https://images.vinted.net/main.jpg"
