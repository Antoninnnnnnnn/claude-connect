"""HTML/JS parsers for lacentrale.fr.

These are the pieces that break silently when the site changes its markup: they
all degrade to None or an empty list rather than raising, so without tests a
regression only shows up as suddenly empty API responses.
"""

import pytest

from app.centrale_client import (
    CentraleError,
    apollo_ad_to_item,
    classified_scripts_to_item,
    discover_listing_js_urls,
    extract_api_key_from_js,
    extract_ev_metadata,
    extract_json_object,
    extract_listing_refs,
    extract_numeric_listing_refs,
    extract_search_card_refs,
    find_apollo_ad,
    listing_url_for_ref,
    parse_script_var_json,
    scan_ssr_markers,
    validate_lacentrale_url,
)

# ------------------------------------------------------------ extract_json_object


def test_extract_simple_object():
    assert extract_json_object('prefix {"a": 1} suffix', 0) == {"a": 1}


def test_extract_balances_nested_braces():
    source = 'x = {"a": {"b": {"c": 1}}, "d": 2}'
    assert extract_json_object(source, 0) == {"a": {"b": {"c": 1}}, "d": 2}


def test_extract_ignores_braces_inside_strings():
    source = 'x = {"label": "a { b } c", "n": 1}'
    assert extract_json_object(source, 0) == {"label": "a { b } c", "n": 1}


def test_extract_handles_escaped_quotes():
    source = r'x = {"label": "say \"hi\" {", "n": 1}'
    assert extract_json_object(source, 0) == {"label": 'say "hi" {', "n": 1}


def test_extract_handles_escaped_backslash_before_quote():
    source = r'x = {"path": "C:\\", "n": 1}'
    assert extract_json_object(source, 0) == {"path": "C:\\", "n": 1}


def test_extract_starts_at_offset():
    source = '{"first": 1} then {"second": 2}'
    assert extract_json_object(source, 12) == {"second": 2}


def test_extract_returns_none_without_object():
    assert extract_json_object("no braces here", 0) is None


def test_extract_returns_none_on_unbalanced():
    assert extract_json_object('{"a": 1', 0) is None


def test_extract_returns_none_on_malformed_json():
    assert extract_json_object("{not json at all}", 0) is None


def test_extract_rejects_json_array_root():
    """A top-level array is not a dict and must be refused."""
    assert extract_json_object('{"a": [1,2]}', 0) == {"a": [1, 2]}


# ----------------------------------------------------------- parse_script_var_json


@pytest.mark.parametrize(
    "assignment",
    [
        'var CLASSIFIED_MAIN_INFOS= {"data": {"x": 1}}',
        'var CLASSIFIED_MAIN_INFOS = {"data": {"x": 1}}',
        'const CLASSIFIED_MAIN_INFOS   =   {"data": {"x": 1}}',
        'let CLASSIFIED_MAIN_INFOS={"data": {"x": 1}}',
        'window.CLASSIFIED_MAIN_INFOS = {"data": {"x": 1}}',
    ],
)
def test_parse_script_var_accepts_assignment_styles(assignment):
    """Live pages emit no space before `=`; the match must stay loose."""
    assert parse_script_var_json(f"<script>{assignment}</script>", "CLASSIFIED_MAIN_INFOS") == {
        "data": {"x": 1}
    }


def test_parse_script_var_missing_returns_none():
    assert parse_script_var_json("<script>var OTHER = {}</script>", "CLASSIFIED_MAIN_INFOS") is None


def test_parse_script_var_skips_non_object_assignment():
    html = 'var X = "a string"; var X = {"real": 1}'
    assert parse_script_var_json(html, "X") == {"real": 1}


def test_parse_script_var_does_not_match_substring_names():
    html = 'var MY_CLASSIFIED_MAIN_INFOS = {"wrong": 1}'
    assert parse_script_var_json(html, "CLASSIFIED_MAIN_INFOS") is None


# ------------------------------------------------------------- listing reference


def test_listing_url_encodes_letter_as_ascii_code():
    """The historical `...-E119617689.html` form 404s; `E` becomes `69`."""
    assert listing_url_for_ref("E119617689") == (
        "https://www.lacentrale.fr/auto-occasion-annonce-69119617689.html"
    )


def test_listing_url_uppercases_input():
    assert listing_url_for_ref("e119617689").endswith("-69119617689.html")


def test_listing_url_passes_through_unrecognised_shape():
    assert listing_url_for_ref("12345").endswith("-12345.html")


@pytest.mark.parametrize(
    ("letter", "code"), [("A", 65), ("B", 66), ("E", 69), ("W", 87), ("Z", 90)]
)
def test_listing_url_roundtrips_through_numeric_extraction(letter, code):
    ref = f"{letter}102941021"
    url = listing_url_for_ref(ref)
    assert f"-{code}102941021.html" in url
    # Only codes 65..90 are matched by the numeric regex, so B/E/W must round-trip.
    if 65 <= code <= 90:
        assert extract_numeric_listing_refs(url) == [ref]


def test_extract_numeric_refs_from_page():
    html = (
        '<a href="/auto-occasion-annonce-69119617689.html">x</a>'
        '<a href="/auto-occasion-annonce-87102941021.html">y</a>'
    )
    assert extract_numeric_listing_refs(html) == ["E119617689", "W102941021"]


def test_extract_search_card_refs():
    html = """
    <div class="searchCard something">
      <a href="/auto-occasion-annonce-B104008382.html">card</a>
    </div>
    """
    assert extract_search_card_refs(html) == ["B104008382"]


def test_extract_search_card_ignores_links_outside_cards():
    html = '<div class="footer"><a href="/auto-occosion-annonce-B1.html">x</a></div>'
    assert extract_search_card_refs(html) == []


def test_extract_listing_refs_merges_and_dedupes_sources():
    html = (
        '<div class="searchCard"><a href="/auto-occasion-annonce-B104008382.html">a</a></div>'
        '<img src="https://pictures.lacentrale.fr/classifieds/E119827940_STANDARD_0.jpg">'
        '<a href="/auto-occasion-annonce-87102941021.html">c</a>'
        '<a href="/auto-occasion-annonce-B104008382.html">dup</a>'
    )
    assert extract_listing_refs(html) == ["B104008382", "E119827940", "W102941021"]


def test_extract_listing_refs_empty_page():
    assert extract_listing_refs("<html><body>nothing</body></html>") == []


# ------------------------------------------------------------------ EV metadata


def test_ev_metadata_reads_battery_and_motor():
    result = extract_ev_metadata(
        title="RENAULT ZOE Q210 88 INTENS 22KWH", version="Q210 88 INTENS 22KWH", energy="ELECTRIC"
    )
    assert result["battery_capacity_kwh"] == 22
    assert result["charge_type"] == "Q"


def test_ev_metadata_accepts_spaced_kwh():
    result = extract_ev_metadata(title="ZOE 41 kWh", version=None, energy="ELECTRIC")
    assert result["battery_capacity_kwh"] == 41


def test_ev_metadata_infers_electric_from_kwh_without_energy():
    result = extract_ev_metadata(title="ZOE 52KWH", version=None, energy=None)
    assert result["battery_capacity_kwh"] == 52


def test_ev_metadata_skips_non_electric():
    assert extract_ev_metadata(title="CLIO 1.5 DCI", version="DCI 90", energy="DIESEL") == {}


def test_ev_metadata_empty_input():
    assert extract_ev_metadata(title=None, version=None, energy="ELECTRIC") == {}


def test_ev_metadata_detects_battery_lease():
    result = extract_ev_metadata(
        title="ZOE 22KWH", version=None, description="Location de batterie 79 euros par mois",
        energy="ELECTRIC",
    )
    assert result["battery_ownership"] == "lease"


def test_ev_metadata_detects_battery_purchase():
    result = extract_ev_metadata(
        title="ZOE 22KWH", version=None, description="Achat integral, batterie incluse",
        energy="ELECTRIC",
    )
    assert result["battery_ownership"] == "purchase"


def test_ev_metadata_battery_ownership_unknown():
    result = extract_ev_metadata(
        title="ZOE 22KWH", version=None, description="La batterie est en bon etat",
        energy="ELECTRIC",
    )
    assert result["battery_ownership"] == "unknown"


def test_ev_metadata_purchase_wins_over_lease_mention():
    result = extract_ev_metadata(
        title="ZOE 22KWH", version=None,
        description="Achat integral, pas de location de batterie", energy="ELECTRIC",
    )
    assert result["battery_ownership"] == "purchase"


def test_ev_metadata_no_ownership_signal():
    result = extract_ev_metadata(title="ZOE 22KWH", version=None, energy="ELECTRIC")
    assert "battery_ownership" not in result


# --------------------------------------------------------------- JS bundle / key


def test_discover_listing_js_urls_finds_hashed_bundle():
    html = '<script src="/fragments/recherche-fragment-front/listing-abc123.js"></script>'
    urls = discover_listing_js_urls(html)
    assert "https://www.lacentrale.fr/fragments/recherche-fragment-front/listing-abc123.js" in urls


def test_discover_listing_js_always_appends_fallback():
    urls = discover_listing_js_urls("<html></html>")
    assert urls and urls[-1].endswith("listing-7df45909.js")


def test_discover_listing_js_dedupes():
    html = (
        '<script src="/fragments/recherche-fragment-front/listing-abc123.js"></script>'
        '<script src="/fragments/recherche-fragment-front/listing-abc123.js"></script>'
    )
    assert len(discover_listing_js_urls(html)) == len(set(discover_listing_js_urls(html)))


@pytest.mark.parametrize(
    "js",
    [
        'headers:{"x-api-key":"ABCDEFGHIJKLMNOPQRSTUVWX"}',
        "const c={apiKey:'ABCDEFGHIJKLMNOPQRSTUVWX'}",
        'x-api-key = "ABCDEFGHIJKLMNOPQRSTUVWX"',
    ],
)
def test_extract_api_key_from_js(js):
    assert extract_api_key_from_js(js) == "ABCDEFGHIJKLMNOPQRSTUVWX"


def test_extract_api_key_requires_min_length():
    assert extract_api_key_from_js('x-api-key:"tooshort"') is None


def test_extract_api_key_absent():
    assert extract_api_key_from_js("var a = 1;") is None


# ----------------------------------------------------------------- Apollo state


def test_find_apollo_ad_by_reference():
    payload = {"props": {"pageProps": {"__APOLLO_STATE__": {"Ad:E1": {"price": 7990}}}}}
    assert find_apollo_ad(payload, "E1") == {"price": 7990}


def test_find_apollo_ad_missing_reference():
    payload = {"props": {"pageProps": {"__APOLLO_STATE__": {"Ad:OTHER": {}}}}}
    assert find_apollo_ad(payload, "E1") is None


@pytest.mark.parametrize(
    "payload",
    [{}, {"props": None}, {"props": {}}, {"props": {"pageProps": {}}},
     {"props": {"pageProps": {"__APOLLO_STATE__": "nope"}}}],
)
def test_find_apollo_ad_tolerates_missing_layers(payload):
    assert find_apollo_ad(payload, "E1") is None


def test_apollo_ad_to_item_maps_fields():
    ad = {
        "price": 7990,
        "description": "Belle voiture",
        "vehicle": {"make": "RENAULT", "technicalSheetUrl": "https://x/sheet"},
        "seller": {"name": "Garage"},
        "criterias": [{"label": "Garantie 12 mois"}, {"label": "Non fumeur"}, {"nolabel": 1}],
    }
    item = apollo_ad_to_item(ad, "E1")
    assert item["reference"] == "E1"
    assert item["price"] == 7990
    assert item["customer"] == {"name": "Garage"}
    assert item["features"] == "Garantie 12 mois, Non fumeur"
    assert item["technical_sheet_url"] == "https://x/sheet"


def test_apollo_ad_to_item_flattens_equipment_categories():
    ad = {
        "vehicle": {
            "equipments": [
                {"items": [{"label": "GPS"}, {"label": "Camera"}]},
                "Bluetooth",
                {"items": [{"nolabel": 1}]},
            ]
        }
    }
    item = apollo_ad_to_item(ad, "E1")
    assert item["equipment"] == "GPS, Camera, Bluetooth"


def test_apollo_ad_to_item_minimal():
    item = apollo_ad_to_item({}, "E1")
    assert item["reference"] == "E1"
    assert "equipment" not in item


# ------------------------------------------------------ classified script blocks


def test_classified_scripts_to_item_reads_main_infos():
    html = """
    <script>var CLASSIFIED_MAIN_INFOS= {"data": {
      "classified": {"price": 7990, "mileage": 56921,
                     "description": {"content": "<p>Tres <b>propre</b></p>"}},
      "vehicle": {"make": "RENAULT", "model": "ZOE"},
      "strengths": [{"label": "Garantie", "value": "12 mois"}, {"label": "Non fumeur"}]
    }}</script>
    """
    item = classified_scripts_to_item(html, "E1")
    assert item["price"] == 7990
    assert item["vehicle"]["mileage"] == 56921
    assert item["vehicle"]["make"] == "RENAULT"
    assert "<b>" not in item["description"], "HTML tags must be stripped"
    assert "Tres" in item["description"] and "propre" in item["description"]
    assert item["features"] == "Garantie: 12 mois | Non fumeur"


def test_classified_scripts_reads_seller_from_more_infos():
    html = """
    <script>var CLASSIFIED_MORE_INFOS = {"data": {"sellerInfos": {
      "sellerName": "Garage Test", "isPro": true, "pack": "PREMIUM",
      "address": {"street1": "1 rue X", "city": "Evreux", "zipCode": "27000", "country": "FR"}
    }}}</script>
    """
    item = classified_scripts_to_item(html, "E1")
    assert item["customer"]["name"] == "Garage Test"
    assert item["customer"]["is_pro"] is True
    assert item["location"]["city"] == "Evreux"
    assert item["location"]["zipCode"] == "27000"


def test_classified_scripts_falls_back_to_summary_for_seller():
    """Older page variants carried sellerInfos in SummaryInformationData."""
    html = (
        '<script>var SummaryInformationData = '
        '{"sellerInfos": {"sellerName": "Vieux Garage", "isPro": false}}</script>'
    )
    item = classified_scripts_to_item(html, "E1")
    assert item["customer"]["name"] == "Vieux Garage"


def test_classified_scripts_prefers_more_infos_over_summary():
    html = (
        '<script>var CLASSIFIED_MORE_INFOS = {"data": {"sellerInfos": {"sellerName": "Nouveau"}}}</script>'
        '<script>var SummaryInformationData = {"sellerInfos": {"sellerName": "Ancien"}}</script>'
    )
    assert classified_scripts_to_item(html, "E1")["customer"]["name"] == "Nouveau"


def test_classified_scripts_plain_string_description():
    html = '<script>var CLASSIFIED_MAIN_INFOS = {"data": {"classified": {"description": "texte"}}}</script>'
    assert classified_scripts_to_item(html, "E1")["description"] == "texte"


def test_classified_scripts_returns_none_without_usable_data():
    assert classified_scripts_to_item("<html>nothing</html>", "E1") is None


# ------------------------------------------------------------------ SSR markers


def test_scan_ssr_markers_detects_known_shells():
    html = "window.__PRELOADED_STATE_LISTING__ = {}; <script id=__NEXT_DATA__>{}</script>"
    found = scan_ssr_markers(html)
    assert "__PRELOADED_STATE_LISTING__" in found
    assert "__NEXT_DATA__" in found


def test_scan_ssr_markers_none():
    assert scan_ssr_markers("<html></html>") == []


# ------------------------------------------------------------------ URL guard


@pytest.mark.parametrize(
    "url",
    [
        "https://www.lacentrale.fr/listing?x=1",
        "https://lacentrale.fr/listing",
        "https://recherche.lacentrale.fr/v5/search",
        "http://www.lacentrale.fr/listing",
    ],
)
def test_validate_url_accepts_lacentrale_hosts(url):
    assert validate_lacentrale_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/listing",
        "https://lacentrale.fr.evil.example/x",
        "https://notlacentrale.fr/x",
        "file:///etc/passwd",
        "ftp://www.lacentrale.fr/x",
        "//www.lacentrale.fr/x",
    ],
)
def test_validate_url_rejects_everything_else(url):
    """This guard is what stops the `url` query param becoming an SSRF vector."""
    with pytest.raises(CentraleError):
        validate_lacentrale_url(url)


def test_validate_url_strips_surrounding_whitespace():
    assert validate_lacentrale_url("  https://www.lacentrale.fr/listing  ") == (
        "https://www.lacentrale.fr/listing"
    )
