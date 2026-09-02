"""Filter -> upstream query mapping, plus hit normalisation.

Two bugs already shipped from this area: the search API is 0-indexed while our
API is 1-indexed, and `distance` is a UI bucket key rather than kilometres.
Both are pinned below.
"""

import pytest
from pydantic import ValidationError

from app.centrale_client import CentraleClient, CentraleError
from app.params import SearchFilters, effective_distance, max_model_year, validate_ranges
from fastapi import HTTPException

# ------------------------------------------------------------------- pagination


def test_page_is_zero_indexed_upstream(client):
    """Our page 1 is upstream page 0."""
    assert client._api_params(page=1)["page"] == "0"
    assert client._api_params(page=2)["page"] == "1"
    assert client._api_params(page=5)["page"] == "4"


def test_page_defaults_to_first(client):
    assert client._api_params()["page"] == "0"


# --------------------------------------------------------------------- defaults


def test_families_default_covers_cars_and_vans(client):
    assert client._api_params()["families"] == "AUTO,UTILITY"


def test_families_override(client):
    assert client._api_params(families="AUTO")["families"] == "AUTO"


def test_sort_default_is_newest(client):
    assert client._api_params()["sortBy"] == "firstOnlineDateDesc"


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("newest", "firstOnlineDateDesc"),
        ("oldest", "firstOnlineDateAsc"),
        ("price_low", "priceAsc"),
        ("price_high", "priceDesc"),
        ("mileage_low", "mileageAsc"),
    ],
)
def test_sort_mapping(client, sort, expected):
    assert client._api_params(sort=sort)["sortBy"] == expected


def test_unknown_sort_falls_back(client):
    assert client._api_params(sort="bogus")["sortBy"] == "firstOnlineDateDesc"


# ------------------------------------------------------------------ make/model


@pytest.mark.parametrize(
    ("make", "model", "expected"),
    [
        ("renault", "zoe", "RENAULT::ZOE"),
        ("renault", None, "RENAULT::"),
        (None, "zoe", "::ZOE"),
        (None, None, None),
        ("  renault  ", " zoe ", "RENAULT::ZOE"),
    ],
)
def test_make_model_format(make, model, expected):
    assert CentraleClient._make_model(make, model) == expected


def test_make_model_lands_in_query(client):
    params = client._api_params(make="RENAULT", model="ZOE")
    assert params["makesModelsCommercialNames"] == "RENAULT::ZOE"


def test_make_model_absent_key_when_unset(client):
    assert "makesModelsCommercialNames" not in client._api_params()


# ------------------------------------------------------------ numeric coercion


def test_float_bounds_become_ints(client):
    params = client._api_params(price_min=8000.0, price_max=12000.0)
    assert params["priceMin"] == "8000"
    assert params["priceMax"] == "12000"


def test_non_integral_float_kept(client):
    assert client._api_params(price_max=8000.5)["priceMax"] == "8000.5"


def test_zero_bound_is_not_dropped(client):
    """0 is falsy but meaningful: `mileageMin=0` must survive."""
    assert client._api_params(mileage_min=0)["mileageMin"] == "0"


def test_absent_bounds_omitted(client):
    params = client._api_params()
    for key in ("priceMin", "priceMax", "yearMin", "yearMax", "mileageMin", "mileageMax"):
        assert key not in params


# --------------------------------------------------------------- zip + distance


def test_zip_without_distance_has_no_bucket(client):
    params = client._api_params(zip_code="27000")
    assert params["zipCode"] == "27000"
    assert "zipCodeDistance" not in params


def test_distance_is_a_ui_bucket_not_kilometres(client):
    """`distance=5` means the 200km bucket in the site UI, not 5km."""
    params = client._api_params(zip_code="27000", distance_km=5)
    assert params["zipCodeDistance"] == "200km"


@pytest.mark.parametrize(
    ("bucket", "expected"),
    [(0, "0km"), (10, "10km"), (20, "20km"), (30, "30km"), (50, "50km"), (100, "100km"), (200, "200km")],
)
def test_known_distance_buckets(client, bucket, expected):
    params = client._api_params(zip_code="27000", distance_km=bucket)
    assert params["zipCodeDistance"] == expected


def test_unknown_bucket_falls_back_to_literal_km(client):
    params = client._api_params(zip_code="27000", distance_km=77)
    assert params["zipCodeDistance"] == "77km"


def test_distance_ignored_without_zip(client):
    assert "zipCodeDistance" not in client._api_params(distance_km=50)


def test_effective_distance_prefers_explicit_bucket():
    assert effective_distance(50, 200) == 200
    assert effective_distance(50, None) == 50
    assert effective_distance(None, None) is None


# ------------------------------------------------------------- passthrough map


@pytest.mark.parametrize(
    ("kwarg", "value", "api_key"),
    [
        ("good_deal", "GOOD_DEAL", "goodDealBadges"),
        ("customer_family", "PARTICULIER", "customerFamilyCodes"),
        ("energy", "ELECTRIC", "energies"),
        ("gearbox", "AUTO", "gearbox"),
        ("body_type", "SUV", "categories"),
        ("color", "NOIR", "externalColors"),
        ("internal_color", "CUIR", "internalColors"),
        ("options", "GPS", "options"),
        ("regions", "FR-IDF", "regions"),
        ("equipment_level", "INTENS", "equipmentLevel"),
        ("critair", "2", "CRITAIR_MAX"),
        ("max_consumption", "5", "MAX_CONSUMPTION"),
        ("freetext", "premiere main", "freetext"),
    ],
)
def test_scalar_filters_are_renamed(client, kwarg, value, api_key):
    assert client._api_params(**{kwarg: value})[api_key] == value


@pytest.mark.parametrize(
    ("kwarg", "value", "api_key"),
    [("co2_max", 100, "co2Max"), ("doors", 5, "doors"), ("power", 90, "power"), ("seats", 5, "seats")],
)
def test_numeric_filters_are_renamed(client, kwarg, value, api_key):
    assert client._api_params(**{kwarg: value})[api_key] == str(value)


@pytest.mark.parametrize(("value", "expected"), [(True, "true"), (False, "false")])
def test_four_wheel_is_stringified(client, value, expected):
    assert client._api_params(four_wheel=value)["fourWheel"] == expected


def test_four_wheel_false_is_not_dropped(client):
    assert "fourWheel" in client._api_params(four_wheel=False)


# --------------------------------------------------- site URL -> API translation


def test_site_params_translate_dptcp_and_distance(client):
    api = client._site_params_to_api(
        {"dptCp": "27000", "distance": "5", "sortBy": "priceAsc"},
        fallback={},
        page=2,
    )
    assert api["zipCode"] == "27000"
    assert api["zipCodeDistance"] == "200km"
    assert api["sortBy"] == "priceAsc"
    assert api["page"] == "1", "site page 2 is upstream page 1"


def test_site_params_rename_versions_to_version(client):
    api = client._site_params_to_api({"versions": "Q210"}, fallback={}, page=1)
    assert api["version"] == "Q210"


def test_site_params_non_numeric_distance_passes_through(client):
    api = client._site_params_to_api({"dptCp": "27000", "distance": "abc"}, fallback={}, page=1)
    assert api["zipCodeDistance"] == "abc"


def test_site_params_fall_back_to_filters(client):
    api = client._site_params_to_api({}, fallback={"make": "RENAULT", "model": "ZOE"}, page=1)
    assert api["makesModelsCommercialNames"] == "RENAULT::ZOE"


def test_site_params_url_wins_over_fallback(client):
    api = client._site_params_to_api(
        {"makesModelsCommercialNames": "PEUGEOT::208"},
        fallback={"make": "RENAULT", "model": "ZOE"},
        page=1,
    )
    assert api["makesModelsCommercialNames"] == "PEUGEOT::208"


def test_site_params_blank_value_does_not_override(client):
    api = client._site_params_to_api(
        {"makesModelsCommercialNames": ""}, fallback={"make": "RENAULT"}, page=1
    )
    assert api["makesModelsCommercialNames"] == "RENAULT::"


# --------------------------------------------------------------- listing paging


def test_with_page_adds_and_removes():
    base = "https://www.lacentrale.fr/listing?families=AUTO"
    assert "page=3" in CentraleClient._with_page(base, 3)
    assert "page" not in CentraleClient._with_page(f"{base}&page=3", 1)


# ------------------------------------------------------------ filter validation


def test_sort_validated_at_the_model():
    with pytest.raises(ValidationError):
        SearchFilters(sort="not-a-sort")


def test_sort_is_lowercased():
    assert SearchFilters(sort="PRICE_LOW").sort == "price_low"


def test_year_bound_is_dynamic():
    """Regression: the bound used to freeze at import time."""
    assert SearchFilters(year_max=max_model_year()).year_max == max_model_year()
    with pytest.raises(ValidationError, match="year must be <="):
        SearchFilters(year_max=max_model_year() + 1)


def test_year_lower_bound_enforced():
    with pytest.raises(ValidationError):
        SearchFilters(year_min=1800)


def test_client_kwargs_mirrors_distance_into_both_keys():
    kwargs = SearchFilters(distance_bucket=200).client_kwargs()
    assert kwargs["distance_km"] == 200 and kwargs["distance_bucket"] == 200


@pytest.mark.parametrize(
    "kwargs",
    [
        {"price_min": 100, "price_max": 50},
        {"year_min": 2020, "year_max": 2010},
        {"mileage_min": 100000, "mileage_max": 1000},
    ],
)
def test_inverted_ranges_rejected(kwargs):
    args = {
        "price_min": None, "price_max": None, "year_min": None,
        "year_max": None, "mileage_min": None, "mileage_max": None,
    }
    args.update(kwargs)
    with pytest.raises(HTTPException) as excinfo:
        validate_ranges(**args)
    assert excinfo.value.status_code == 422


def test_valid_ranges_pass():
    validate_ranges(50, 100, 2010, 2020, 1000, 100000)


def test_client_rejects_unknown_sort(client):
    with pytest.raises(CentraleError, match="Invalid sort"):
        client._search_filter_kwargs(sort="nope")


def test_client_filter_kwargs_drops_nones(client):
    clean = client._search_filter_kwargs(make="RENAULT", model=None, sort="newest")
    assert "model" not in clean and clean["make"] == "RENAULT"


# ----------------------------------------------------------- hit normalisation


@pytest.fixture
def raw_hit() -> dict:
    return {
        "item": {
            "reference": "E119827940",
            "price": 7990,
            "goodDealBadge": "BAD_DEAL",
            "photoUrl": "https://pictures.example/a.jpg",
            "description": "Vehicule suivi",
            "vehicle": {
                "make": "RENAULT", "model": "ZOE", "version": "88 LIFE 22KWH",
                "year": 2015, "mileage": 56921, "energy": "ELECTRIC", "gearbox": "AUTO",
            },
            "customer": {"family": "CONCESSIONNAIRE", "name": "Garage", "city": "Evreux"},
        }
    }


def normalize(client, hit, **flags):
    defaults = dict(include_image=False, include_dealer=False, include_vehicle=False, raw=False)
    defaults.update(flags)
    return client._normalize_hit(hit, **defaults)


def test_normalize_core_fields(client, raw_hit):
    result = normalize(client, raw_hit)
    assert result.id == "E119827940"
    assert result.make == "RENAULT" and result.model == "ZOE"
    assert result.year == 2015 and result.mileage == 56921
    assert result.price == 7990.0
    assert result.dealer_type == "CONCESSIONNAIRE"
    assert result.good_deal_badge == "BAD_DEAL"


def test_normalize_builds_title_from_parts(client, raw_hit):
    assert normalize(client, raw_hit).title == "RENAULT ZOE 88 LIFE 22KWH"


def test_normalize_prefers_explicit_title(client, raw_hit):
    raw_hit["item"]["title"] = "Titre force"
    assert normalize(client, raw_hit).title == "Titre force"


def test_normalize_derives_url_from_reference(client, raw_hit):
    assert normalize(client, raw_hit).url.endswith("-69119827940.html")


def test_normalize_keeps_supplied_url(client, raw_hit):
    raw_hit["item"]["url"] = "https://www.lacentrale.fr/x.html"
    assert normalize(client, raw_hit).url == "https://www.lacentrale.fr/x.html"


def test_normalize_extracts_ev_fields(client, raw_hit):
    assert normalize(client, raw_hit).battery_capacity_kwh == 22


def test_normalize_gates_verbose_fields(client, raw_hit):
    result = normalize(client, raw_hit)
    assert result.image is None
    assert result.dealer is None
    assert result.vehicle is None
    assert result.description is None
    assert result.raw is None


def test_normalize_opt_ins(client, raw_hit):
    result = normalize(client, raw_hit, include_image=True, include_dealer=True, include_vehicle=True)
    assert result.image == "https://pictures.example/a.jpg"
    assert result.dealer["name"] == "Garage"
    assert result.vehicle["make"] == "RENAULT"


def test_normalize_description_requires_flag(client, raw_hit):
    assert client._normalize_hit(
        raw_hit, include_image=False, include_dealer=False, include_vehicle=False,
        include_description=True, raw=False,
    ).description == "Vehicule suivi"


def test_normalize_accepts_bare_item(client):
    """SSR paths hand over the item without the `item` envelope."""
    result = normalize(client, {"reference": "B1", "price": 100})
    assert result.id == "B1" and result.price == 100.0


def test_normalize_builds_location_from_loose_keys(client):
    result = normalize(client, {"item": {"reference": "B1", "zipCode": "27000", "city": "Evreux"}})
    assert result.location == {"zipCode": "27000", "city": "Evreux"}


def test_normalize_empty_hit(client):
    result = normalize(client, {})
    assert result.id is None and result.price is None


def test_normalize_bad_price_is_none(client):
    assert normalize(client, {"item": {"reference": "B1", "price": "n/a"}}).price is None


# --------------------------------------------------------------- merge helper


def test_merge_prefers_base_and_fills_gaps():
    merged = CentraleClient._merge_listing_items(
        {"price": 100, "title": None}, {"price": 200, "title": "From HTML"}
    )
    assert merged["price"] == 100, "base wins when it has a value"
    assert merged["title"] == "From HTML", "override fills a missing value"


def test_merge_deep_merges_known_dicts():
    merged = CentraleClient._merge_listing_items(
        {"vehicle": {"make": "RENAULT"}, "customer": {"name": "A"}, "location": {"city": "X"}},
        {"vehicle": {"model": "ZOE"}, "customer": {"is_pro": True}, "location": {"zipCode": "27000"}},
    )
    assert merged["vehicle"] == {"make": "RENAULT", "model": "ZOE"}
    assert merged["customer"] == {"name": "A", "is_pro": True}
    assert merged["location"] == {"city": "X", "zipCode": "27000"}


def test_merge_skips_none_and_empty_values():
    merged = CentraleClient._merge_listing_items({"a": ""}, {"a": "filled", "b": None})
    assert merged["a"] == "filled"
    assert "b" not in merged


def test_merge_tolerates_non_dicts():
    assert CentraleClient._merge_listing_items(None, {"a": 1}) == {"a": 1}
    assert CentraleClient._merge_listing_items({"a": 1}, None) == {"a": 1}
