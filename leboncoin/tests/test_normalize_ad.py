"""Ad normalisation: the mapping that silently degrades when Leboncoin renames a field."""

import pytest

from app.lbc_client import LeboncoinClient


@pytest.fixture
def raw_ad() -> dict:
    """Shape mirrors a /finder/search hit, trimmed to the fields we read."""
    return {
        "list_id": 3261595762,
        "subject": "Baskets Nike T37,5",
        "body": "Tres bon etat, portees deux fois.",
        "brand": "leboncoin",
        "category_id": 53,
        "category_name": "Chaussures",
        "ad_type": "offer",
        "status": "active",
        "price_cents": 5900,
        "price": [59],
        "url": "/ad/chaussures/3261595762",
        "first_publication_date": "2026-09-02 15:11:45",
        "index_date": "2026-09-02 15:11:45",
        "images": {
            "nb_images": 12,
            "urls_large": ["https://img.example/large1.jpg", "https://img.example/large2.jpg"],
            "thumb_url": "https://img.example/thumb.jpg",
        },
        "location": {
            "region_name": "Nord-Pas-de-Calais",
            "department_name": "Nord",
            "city_label": "Ville",
            "zipcode": "59000",
            "lat": 50.63,
            "lng": 3.06,
        },
        "owner": {"user_id": "u1", "name": "Vendeur", "type": "private", "siren": None},
        "options": {"urgent": True, "gallery": False},
        "is_boosted": True,
        "attributes": [
            {"key": "condition", "key_label": "Etat", "value": "2", "value_label": "Tres bon etat"},
            {"key": "shoe_brand", "value_label": "Nike"},
            {"key": "old_price", "value": "75"},
            {"key": "rating_score", "value": "4.8"},
            {"key": "rating_count", "value": "37"},
            {"key": "shippable", "value": "true"},
            {"key": "shipping_type", "values_label": ["mondial_relay", "colissimo"]},
            {"key": "estimated_parcel_weight", "value": "900"},
        ],
    }


def normalize(client, ad, **flags):
    defaults = dict(
        include_image=False,
        include_images=False,
        include_body=False,
        include_owner=False,
        include_attributes=False,
        include_coordinates=False,
        include_raw=False,
    )
    defaults.update(flags)
    return client._normalize_ad(ad, **defaults)


def test_core_fields(client, raw_ad):
    result = normalize(client, raw_ad)
    assert result.id == 3261595762
    assert result.title == "Baskets Nike T37,5"
    assert result.category_id == "53"
    assert result.category_name == "Chaussures"
    assert result.ad_type == "offer"
    assert result.status == "active"
    assert result.currency == "EUR"


def test_price_cents_wins_over_price_list(client, raw_ad):
    assert normalize(client, raw_ad).price == 59.0


def test_price_falls_back_to_list(client, raw_ad):
    raw_ad.pop("price_cents")
    assert normalize(client, raw_ad).price == 59.0


def test_price_missing_is_none(client, raw_ad):
    raw_ad.pop("price_cents")
    raw_ad.pop("price")
    assert normalize(client, raw_ad).price is None


def test_price_garbage_is_none(client, raw_ad):
    raw_ad.pop("price_cents")
    raw_ad["price"] = ["n/a"]
    assert normalize(client, raw_ad).price is None


def test_url_is_absolutised(client, raw_ad):
    assert normalize(client, raw_ad).url == "https://www.leboncoin.fr/ad/chaussures/3261595762"


def test_absolute_url_kept(client, raw_ad):
    raw_ad["url"] = "https://www.leboncoin.fr/ad/x/1"
    assert normalize(client, raw_ad).url == "https://www.leboncoin.fr/ad/x/1"


def test_brand_prefers_attribute_over_placeholder(client, raw_ad):
    """The top-level `brand` is often the literal string "leboncoin"."""
    assert normalize(client, raw_ad).brand == "Nike"


def test_brand_placeholder_alone_is_dropped(client, raw_ad):
    raw_ad["attributes"] = []
    assert normalize(client, raw_ad).brand is None


def test_condition_from_attributes(client, raw_ad):
    assert normalize(client, raw_ad).condition == "Tres bon etat"


def test_old_price_coerced_to_float(client, raw_ad):
    assert normalize(client, raw_ad).old_price == 75.0


def test_seller_rating(client, raw_ad):
    assert normalize(client, raw_ad).seller_rating == {"score": 4.8, "count": 37}


def test_shipping_compacted(client, raw_ad):
    shipping = normalize(client, raw_ad).shipping
    assert shipping["shippable"] is True
    assert shipping["methods"] == ["mondial_relay", "colissimo"]
    assert shipping["parcel_weight_g"] == 900
    assert "parcel_size" not in shipping, "absent attributes must be dropped, not null"


def test_options_and_boost(client, raw_ad):
    options = normalize(client, raw_ad).options
    assert options["urgent"] is True
    assert options["gallery"] is False
    assert options["is_boosted"] is True


def test_image_count_uses_nb_images(client, raw_ad):
    assert normalize(client, raw_ad).image_count == 12


def test_image_count_falls_back_to_list_length(client, raw_ad):
    raw_ad["images"].pop("nb_images")
    assert normalize(client, raw_ad).image_count == 2


def test_images_prefer_urls_large(client, raw_ad):
    result = normalize(client, raw_ad, include_images=True)
    assert result.images == ["https://img.example/large1.jpg", "https://img.example/large2.jpg"]


def test_images_fall_back_to_thumb_string(client, raw_ad):
    raw_ad["images"] = {"thumb_url": "https://img.example/thumb.jpg"}
    result = normalize(client, raw_ad, include_images=True)
    assert result.images == ["https://img.example/thumb.jpg"]


def test_missing_images_block(client, raw_ad):
    raw_ad.pop("images")
    result = normalize(client, raw_ad, include_images=True, include_image=True)
    assert result.images is None and result.image is None


# ------------------------------------------------- opt-in field gating (context)


def test_verbose_fields_off_by_default(client, raw_ad):
    result = normalize(client, raw_ad)
    assert result.body is None
    assert result.image is None
    assert result.images is None
    assert result.owner is None
    assert result.attributes is None
    assert result.attributes_map is None
    assert result.raw is None


def test_body_opt_in(client, raw_ad):
    assert normalize(client, raw_ad, include_body=True).body.startswith("Tres bon etat")


def test_image_opt_in_returns_first_only(client, raw_ad):
    result = normalize(client, raw_ad, include_image=True)
    assert result.image == "https://img.example/large1.jpg"
    assert result.images is None


def test_owner_opt_in_is_compacted(client, raw_ad):
    owner = normalize(client, raw_ad, include_owner=True).owner
    assert owner == {"user_id": "u1", "name": "Vendeur", "type": "private"}
    assert "siren" not in owner, "null keys must be dropped"


def test_coordinates_gated(client, raw_ad):
    without = normalize(client, raw_ad).location
    with_coords = normalize(client, raw_ad, include_coordinates=True).location
    assert "lat" not in without
    assert with_coords["lat"] == 50.63 and with_coords["lng"] == 3.06


def test_attributes_map_opt_in(client, raw_ad):
    mapped = normalize(client, raw_ad, include_attributes=True).attributes_map
    assert mapped["condition"]["value"] == "Tres bon etat"
    assert "key" not in mapped["condition"], "key becomes the dict key"


def test_raw_opt_in(client, raw_ad):
    assert normalize(client, raw_ad, include_raw=True).raw == raw_ad


# ----------------------------------------------------------------- degradation


def test_empty_ad_does_not_raise(client):
    result = normalize(client, {})
    assert result.id is None and result.title is None and result.price is None


def test_malformed_nested_types_are_ignored(client):
    ad = {"list_id": 1, "location": "not-a-dict", "owner": [], "attributes": "nope", "images": []}
    result = normalize(client, ad, include_owner=True, include_attributes=True, include_images=True)
    assert result.location is None
    assert result.owner is None
    assert result.attributes is None
    assert result.images is None


# ------------------------------------------------------------------- utilities


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Vêtements", "vetements"), ("JEUX_VIDEO", "jeux_video"), ("a  b", "a_b"), ("--x--", "x")],
)
def test_key_normalisation(raw, expected):
    assert LeboncoinClient._key(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("oui", True), ("1", True), ("false", False), ("non", False), ("maybe", None), (None, None)],
)
def test_bool_value(raw, expected):
    assert LeboncoinClient._bool_value(raw) is expected


def test_category_info_builds_path(client):
    key, path = client._category_info(53)
    assert key == "mode_chaussures"
    assert path == ["Mode", "Chaussures"]


def test_category_info_unknown_is_none(client):
    assert client._category_info(999999) == (None, None)
