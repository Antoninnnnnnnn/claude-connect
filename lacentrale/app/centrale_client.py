import json
import logging
import math
import os
import random
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from curl_cffi import requests

from app.config import Settings
from app.models import PriceStats, SearchResult


logger = logging.getLogger(__name__)

CENTRALE_HOST = "https://www.lacentrale.fr"
RECHERCHE_HOST = "https://recherche.lacentrale.fr"
GEOLOC_HOST = "https://geoloc.lacentrale.fr"
LISTING_JS_FALLBACK = f"{CENTRALE_HOST}/fragments/recherche-fragment-front/listing-7df45909.js"
LISTING_JS_RE = re.compile(r"/fragments/recherche-fragment-front/listing-[a-f0-9]+\.js")
FRAGMENT_JS_RE = re.compile(r'/fragments/recherche-fragment-front/[^"\'\s>]+\.js')

DATADOME_DESCRIPTION_UNAVAILABLE = "Non disponible (Bloqué par DataDome)"

# Last-resort upstream keys (public front-end keys; override via CENTRALE_UPSTREAM_API_KEY).
UPSTREAM_API_KEY_FALLBACKS: tuple[str, ...] = ()

BATTERY_KWH_RE = re.compile(r"\b(\d{2})\s*kwh\b", re.I)
BATTERY_KWH_COMPACT_RE = re.compile(r"\b(\d{2})kwh\b", re.I)
CHARGE_MOTOR_RE = re.compile(r"\b([QR])(\d{2,3})\b", re.I)
BATTERY_LEASE_RE = re.compile(
    r"location\s+(?:de\s+)?batterie|loyer\s+(?:de\s+)?batterie|batterie\s+lou[eé]e|\bdiac\b",
    re.I,
)
BATTERY_OWNED_RE = re.compile(
    r"achat\s+int[eé]gral|batterie\s+incluse|sans\s+location|propri[eé]t[eé]\s+batterie|batterie\s+achet[eé]e",
    re.I,
)

ALLOWED_URL_HOSTS = frozenset(
    {
        "www.lacentrale.fr",
        "lacentrale.fr",
        "recherche.lacentrale.fr",
    }
)

API_KEY_RE = re.compile(
    r'(?:x-api-key["\']?\s*[:=]\s*["\']|apiKey["\']?\s*:\s*["\'])([A-Za-z0-9]{20,})'
)
ZIP_DISTANCE_RE = re.compile(r'zipCodeDistance["\']?\s*[:=]\s*["\'](\d+km)["\']', re.I)
CLASSIFIED_REF_RE = re.compile(r"classifieds/([A-Z]\d+)_", re.I)
LISTING_REF_RE = re.compile(r"/auto-occasion-annonce-([A-Z]\d+)\.html", re.I)
LISTING_REF_PATTERN = re.compile(r"^[A-Z]\d+$", re.I)
DETAIL_PRICE_RE = re.compile(r'"classifiedPrice"\s*:\s*(\d+)|"price"\s*:\s*(\d+)')
SEARCH_CARD_HREF_RE = re.compile(
    r'<div[^>]*class="[^"]*searchCard[^"]*"[^>]*>.*?<a[^>]+href="(/auto-occasion-annonce-[A-Z]\d+\.html)"',
    re.I | re.S,
)
SCRIPT_VAR_MARKERS = ("CLASSIFIED_MAIN_INFOS", "SummaryInformationData")

SSR_MARKERS = (
    "__PRELOADED_STATE_LISTING__",
    "__NEXT_DATA__",
    "window.__INITIAL_STATE__",
)

SORT_PARAMS = {
    "newest": "firstOnlineDateDesc",
    "recent": "firstOnlineDateDesc",
    "oldest": "firstOnlineDateAsc",
    "price_low": "priceAsc",
    "price_high": "priceDesc",
    "mileage_low": "mileageAsc",
}

GOOD_DEAL_BADGES = (
    "VERY_GOOD_DEAL",
    "GOOD_DEAL",
    "EQUITABLE_DEAL",
    "BAD_DEAL",
    "NOT_COMPUTED",
)

CUSTOMER_FAMILIES = (
    "PARTICULIER",
    "PROFESSIONNEL",
    "COURTIER_AUTOMOBILE",
    "INTERMEDIAIRE",
    "CENTRE_MULTIMARQUES",
)

VEHICLE_FAMILIES = ("AUTO", "UTILITY")

AGGREGATION_FACETS = (
    "REGION",
    "CUSTOMER_FAMILY_CODE",
    "TOP_OPTIONS",
    "EQUIPMENT_LEVEL",
    "EXTERNAL_COLOR",
    "INTERNAL_COLOR",
    "MAX_CONSUMPTION",
    "CRITAIR_MAX",
)

# Site listing URL param → recherche API param (1:1 or renamed)
SITE_TO_API_KEYS: dict[str, str] = {
    "makesModelsCommercialNames": "makesModelsCommercialNames",
    "versions": "version",
    "version": "version",
    "priceMin": "priceMin",
    "priceMax": "priceMax",
    "yearMin": "yearMin",
    "yearMax": "yearMax",
    "mileageMin": "mileageMin",
    "mileageMax": "mileageMax",
    "goodDealBadges": "goodDealBadges",
    "customerFamilyCodes": "customerFamilyCodes",
    "sortBy": "sortBy",
    "page": "page",
    "families": "families",
    "energies": "energies",
    "gearbox": "gearbox",
    "categories": "categories",
    "externalColors": "externalColors",
    "externalColor": "externalColors",
    "internalColors": "internalColors",
    "internalColor": "internalColors",
    "options": "options",
    "equipmentLevel": "equipmentLevel",
    "regions": "regions",
    "co2Max": "co2Max",
    "doors": "doors",
    "power": "power",
    "seats": "seats",
    "fourWheel": "fourWheel",
    "freetext": "freetext",
    "freetext_conversationid": "freetext_conversationid",
    "zipCode": "zipCode",
    "dptCp": "zipCode",
    "distance": "zipCodeDistance",
    "critair": "CRITAIR_MAX",
    "CRITAIR_MAX": "CRITAIR_MAX",
    "maxConsumption": "MAX_CONSUMPTION",
    "MAX_CONSUMPTION": "MAX_CONSUMPTION",
    "fourWheel": "fourWheel",
}

# UI distance index on lacentrale.fr/listing → API zipCodeDistance (capture: distance=5 → 200km)
DISTANCE_UI_TO_API: dict[int, str] = {
    0: "0km",
    5: "200km",
    10: "10km",
    20: "20km",
    30: "30km",
    50: "50km",
    100: "100km",
    200: "200km",
}

PAGE_SIZE = 24
COOKIE_DENYLIST = frozenset({"access-token", "refresh-token"})


class CentraleError(RuntimeError):
    pass


class CentraleNotFoundError(CentraleError):
    pass


def validate_lacentrale_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise CentraleError("url must use http or https")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_URL_HOSTS:
        raise CentraleError("url host must be a lacentrale.fr domain")
    return url.strip()


def extract_json_object(source: str, start_index: int) -> dict[str, Any] | None:
    brace_start = source.find("{", start_index)
    if brace_start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(brace_start, len(source)):
        char = source[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(source[brace_start : index + 1])
                except json.JSONDecodeError:
                    return None
                return data if isinstance(data, dict) else None
    return None


def scan_ssr_markers(html: str) -> list[str]:
    return [marker for marker in SSR_MARKERS if marker in html]


def extract_ev_metadata(
    *,
    title: str | None,
    version: str | None,
    description: str | None = None,
    energy: str | None = None,
) -> dict[str, Any]:
    """Extract EV-oriented fields from title, version and optional description."""
    text = " ".join(
        part for part in (title, version, description) if part
    )
    if not text.strip():
        return {}

    result: dict[str, Any] = {}
    energy_upper = str(energy or "").upper()
    is_electric = energy_upper in {"ELECTRIC", "ELECTRIQUE", "ELECTRIQUE_B"} or bool(
        BATTERY_KWH_RE.search(text) or BATTERY_KWH_COMPACT_RE.search(text) or CHARGE_MOTOR_RE.search(text)
    )
    if not is_electric:
        return result

    for pattern in (BATTERY_KWH_RE, BATTERY_KWH_COMPACT_RE):
        match = pattern.search(text)
        if match:
            result["battery_capacity_kwh"] = int(match.group(1))
            break

    motor_match = CHARGE_MOTOR_RE.search(text)
    if motor_match:
        motor_type = motor_match.group(1).upper()
        result["charge_type"] = motor_type

    normalized = text.lower()
    if BATTERY_OWNED_RE.search(normalized):
        result["battery_ownership"] = "purchase"
    elif BATTERY_LEASE_RE.search(normalized):
        result["battery_ownership"] = "lease"
    elif "batterie" in normalized:
        result["battery_ownership"] = "unknown"

    return result


def discover_listing_js_urls(html: str) -> list[str]:
    """Collect candidate listing JS bundle URLs from a /listing HTML page."""
    urls: list[str] = []
    seen: set[str] = set()
    for pattern in (LISTING_JS_RE, FRAGMENT_JS_RE):
        for match in pattern.finditer(html):
            path = match.group(0)
            if not path.startswith("/"):
                continue
            url = f"{CENTRALE_HOST}{path}"
            if url not in seen:
                seen.add(url)
                urls.append(url)
    if LISTING_JS_FALLBACK not in seen:
        urls.append(LISTING_JS_FALLBACK)
    return urls


def extract_api_key_from_js(js_source: str) -> str | None:
    match = API_KEY_RE.search(js_source)
    return match.group(1) if match else None


def parse_script_var_json(html: str, var_name: str) -> dict[str, Any] | None:
    for prefix in (
        f"var {var_name} = ",
        f"const {var_name} = ",
        f"let {var_name} = ",
        f"window.{var_name} = ",
    ):
        index = html.find(prefix)
        if index >= 0:
            data = extract_json_object(html, index + len(prefix) - 1)
            if data:
                return data
    return None


def find_apollo_ad(payload: dict[str, Any], ref: str) -> dict[str, Any] | None:
    props = payload.get("props", {}).get("pageProps", {}) if isinstance(payload.get("props"), dict) else {}
    apollo = props.get("__APOLLO_STATE__")
    if not isinstance(apollo, dict):
        return None
    direct = apollo.get(f"Ad:{ref}")
    if isinstance(direct, dict):
        return direct
    return None


def apollo_ad_to_item(ad_data: dict[str, Any], ref: str) -> dict[str, Any]:
    vehicle = ad_data.get("vehicle") if isinstance(ad_data.get("vehicle"), dict) else {}
    seller = ad_data.get("seller") if isinstance(ad_data.get("seller"), dict) else {}
    item: dict[str, Any] = {
        "reference": ref,
        "price": ad_data.get("price"),
        "description": ad_data.get("description"),
        "vehicle": vehicle,
        "customer": seller,
    }
    criterias = ad_data.get("criterias")
    if isinstance(criterias, list):
        item["features"] = ", ".join(
            str(c.get("label")).strip()
            for c in criterias
            if isinstance(c, dict) and c.get("label")
        )
    equipments: list[str] = []
    for category in vehicle.get("equipments") or []:
        if isinstance(category, dict):
            for entry in category.get("items") or []:
                if isinstance(entry, dict) and entry.get("label"):
                    equipments.append(str(entry["label"]))
        elif isinstance(category, str):
            equipments.append(category)
    if equipments:
        item["equipment"] = ", ".join(equipments)
    if vehicle.get("technicalSheetUrl"):
        item["technical_sheet_url"] = vehicle.get("technicalSheetUrl")
    return item


def classified_scripts_to_item(html: str, ref: str) -> dict[str, Any] | None:
    item: dict[str, Any] = {"reference": ref}
    main_infos = parse_script_var_json(html, "CLASSIFIED_MAIN_INFOS")
    if isinstance(main_infos, dict):
        data = main_infos.get("data") if isinstance(main_infos.get("data"), dict) else main_infos
        classified = data.get("classified") if isinstance(data.get("classified"), dict) else {}
        vehicle = data.get("vehicle") if isinstance(data.get("vehicle"), dict) else {}
        if classified.get("price") is not None:
            item["price"] = classified.get("price")
        if classified.get("mileage") is not None:
            item.setdefault("vehicle", {})["mileage"] = classified.get("mileage")
        if vehicle:
            item["vehicle"] = {**item.get("vehicle", {}), **vehicle}
        desc = classified.get("description")
        if isinstance(desc, dict) and desc.get("content"):
            item["description"] = re.sub(r"<[^>]+>", " ", str(desc["content"]))
        elif isinstance(desc, str):
            item["description"] = desc
        strengths = data.get("strengths")
        if isinstance(strengths, list):
            feats = []
            for strength in strengths:
                if isinstance(strength, dict) and strength.get("label"):
                    label = strength["label"]
                    value = strength.get("value")
                    feats.append(f"{label}: {value}" if value else str(label))
            if feats:
                item["features"] = " | ".join(feats)

    summary = parse_script_var_json(html, "SummaryInformationData")
    if isinstance(summary, dict):
        seller_infos = summary.get("sellerInfos") if isinstance(summary.get("sellerInfos"), dict) else {}
        if seller_infos.get("sellerName"):
            item["customer"] = {"name": seller_infos.get("sellerName")}
        address = seller_infos.get("address") if isinstance(seller_infos.get("address"), dict) else {}
        if address:
            item["location"] = {
                key: address.get(key)
                for key in ("street1", "city", "zipCode", "country")
                if address.get(key) is not None
            }

    return item if len(item) > 1 else None


def extract_search_card_refs(html: str) -> list[str]:
    refs: list[str] = []
    for href in SEARCH_CARD_HREF_RE.findall(html):
        match = LISTING_REF_RE.search(href)
        if match:
            refs.append(match.group(1).upper())
    return refs


def extract_listing_refs(html: str) -> list[str]:
    return sorted(
        set(extract_search_card_refs(html) + CLASSIFIED_REF_RE.findall(html) + LISTING_REF_RE.findall(html))
    )


class CentraleClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._proxy_index = 0
        self._www_cookies: dict[str, str] = {}
        self._datadome_client_id: str | None = None
        self._json_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._listing_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._metadata_cache: tuple[float, dict[str, Any]] | None = None
        self._distance_buckets: dict[int, str] | None = None
        self._recherche_session: requests.Session | None = None
        self._listing_js_url: str | None = None
        self._upstream_api_key_cached: str | None = None
        self._prime_cache: tuple[str, float] | None = None
        self._load_cookies()

    def bootstrap(self) -> None:
        if self.settings.centrale_upstream_api_key:
            self._upstream_api_key_cached = self.settings.centrale_upstream_api_key

    def max_fetchable_limit(self) -> int:
        return self.settings.max_fetchable_limit()

    def search(
        self,
        *,
        make: str | None = None,
        model: str | None = None,
        version: str | None = None,
        price_min: float | None = None,
        price_max: float | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        mileage_min: int | None = None,
        mileage_max: int | None = None,
        zip: str | None = None,
        distance_km: int | None = None,
        distance_bucket: int | None = None,
        good_deal: str | None = None,
        customer_family: str | None = None,
        energy: str | None = None,
        gearbox: str | None = None,
        body_type: str | None = None,
        color: str | None = None,
        internal_color: str | None = None,
        families: str | None = None,
        options: str | None = None,
        regions: str | None = None,
        equipment_level: str | None = None,
        critair: str | None = None,
        co2_max: int | None = None,
        max_consumption: str | None = None,
        doors: int | None = None,
        power: int | None = None,
        seats: int | None = None,
        four_wheel: bool | None = None,
        freetext: str | None = None,
        sort: str = "newest",
        page: int = 1,
        limit: int = 20,
        url: str | None = None,
        include_image: bool = False,
        include_dealer: bool = False,
        include_vehicle: bool = False,
        debug: bool = False,
        raw: bool = False,
    ) -> dict[str, Any]:
        effective_distance = distance_bucket if distance_bucket is not None else distance_km
        max_limit = self.max_fetchable_limit()
        limit = max(1, min(limit, max_limit))
        pages_to_fetch = max(1, min(math.ceil(limit / PAGE_SIZE), self.settings.centrale_max_pages_per_search))

        filter_kwargs = self._search_filter_kwargs(
            make=make,
            model=model,
            version=version,
            price_min=price_min,
            price_max=price_max,
            year_min=year_min,
            year_max=year_max,
            mileage_min=mileage_min,
            mileage_max=mileage_max,
            zip=zip,
            distance_km=effective_distance,
            good_deal=good_deal,
            customer_family=customer_family,
            energy=energy,
            gearbox=gearbox,
            body_type=body_type,
            color=color,
            internal_color=internal_color,
            families=families,
            options=options,
            regions=regions,
            equipment_level=equipment_level,
            critair=critair,
            co2_max=co2_max,
            max_consumption=max_consumption,
            doors=doors,
            power=power,
            seats=seats,
            four_wheel=four_wheel,
            freetext=freetext,
            sort=sort,
        )

        hits: list[dict[str, Any]] = []
        failures: list[str] = []
        metadata: dict[str, Any] = {}
        source = "unknown"

        for page_number in range(page, page + pages_to_fetch):
            try:
                page_hits, page_meta, page_source = self._fetch_page_hits(
                    page=page_number,
                    url=url,
                    **filter_kwargs,
                )
                hits.extend(page_hits)
                source = page_source
                if not metadata:
                    metadata = page_meta
                total_raw = metadata.get("total")
                if total_raw is not None:
                    try:
                        if len(hits) >= int(total_raw):
                            break
                    except (TypeError, ValueError):
                        pass
            except Exception as exc:
                failures.append(f"page {page_number}: {exc}")
                if page_number == page and not hits:
                    break

        if not hits and failures:
            raise CentraleError("; ".join(failures))

        items = [
            self._normalize_hit(
                hit,
                include_image=include_image,
                include_dealer=include_dealer,
                include_vehicle=include_vehicle,
                raw=raw,
            ).model_dump(exclude_none=True)
            for hit in hits[:limit]
        ]
        data: dict[str, Any] = {
            "items": items,
            "pagination": {
                "page": page,
                "limit": limit,
                "returned": len(items),
                "max_fetchable": max_limit,
                **metadata,
            },
            "source": source,
        }
        if debug:
            data["strategy"] = self._primary_strategy()
        if failures:
            data["failures"] = failures
        return data

    def listing(
        self,
        ref: str,
        *,
        include_image: bool = True,
        include_dealer: bool = False,
        include_vehicle: bool = True,
        include_description: bool = False,
        raw: bool = False,
        debug: bool = False,
    ) -> dict[str, Any]:
        clean_ref = ref.strip().upper()
        if not LISTING_REF_PATTERN.match(clean_ref):
            raise CentraleError(f"Invalid listing reference format: {ref}")

        cache_key = self._listing_cache_key(
            clean_ref,
            include_image=include_image,
            include_dealer=include_dealer,
            include_vehicle=include_vehicle,
            include_description=include_description,
            raw=raw,
            debug=debug,
        )
        cached = self._cached_listing(cache_key)
        if cached is not None:
            return cached

        item_data = self._listing_json(clean_ref)
        source = "json"
        html_warnings: list[str] = []

        if item_data is None:
            html_item, html_error = self._fetch_listing_html_enrichment(clean_ref)
            if html_item:
                item_data = html_item
                source = "html"
            elif html_error:
                html_warnings.append(html_error)
        elif include_description or include_dealer:
            html_item, html_error = self._fetch_listing_html_enrichment(clean_ref)
            if html_item:
                item_data = self._merge_listing_items(item_data, html_item)
                source = "json+html"
            elif html_error:
                html_warnings.append(html_error)
                if include_description and not item_data.get("description"):
                    item_data["description"] = DATADOME_DESCRIPTION_UNAVAILABLE

        if not item_data:
            raise CentraleNotFoundError(f"Listing not found: {clean_ref}")

        normalized = self._normalize_hit(
            {"item": item_data},
            include_image=include_image,
            include_dealer=include_dealer,
            include_vehicle=include_vehicle,
            include_description=include_description,
            raw=raw,
        ).model_dump(exclude_none=True)
        payload: dict[str, Any] = {"item": normalized, "source": source}
        if debug:
            payload["strategy"] = self._primary_strategy()
        if html_warnings:
            payload["html_warnings"] = html_warnings
        self._store_listing(cache_key, payload)
        return payload

    def listings(
        self,
        refs: list[str],
        *,
        max_workers: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        clean_refs = []
        seen: set[str] = set()
        for ref in refs:
            clean = ref.strip().upper()
            if not clean or clean in seen:
                continue
            if not LISTING_REF_PATTERN.match(clean):
                raise CentraleError(f"Invalid listing reference format: {ref}")
            seen.add(clean)
            clean_refs.append(clean)
        if not clean_refs:
            return {"items": [], "errors": {}}

        workers = max(1, min(max_workers or self.settings.centrale_listing_max_workers, len(clean_refs)))
        items: list[dict[str, Any]] = []
        errors: dict[str, str] = {}

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(self.listing, ref, **kwargs): ref for ref in clean_refs
            }
            for future in as_completed(future_map):
                ref = future_map[future]
                try:
                    result = future.result()
                    items.append({"ref": ref, **result})
                except Exception as exc:
                    errors[ref] = str(exc)
                    logger.warning("Parallel listing fetch failed for %s: %s", ref, exc)

        items.sort(key=lambda entry: clean_refs.index(entry["ref"]))
        return {"items": items, "errors": errors}

    def price_stats(self, **kwargs: Any) -> PriceStats:
        max_limit = self.max_fetchable_limit()
        kwargs["limit"] = min(max(int(kwargs.get("limit") or 70), 1), max_limit)
        result = self.search(**kwargs)
        prices = sorted(item["price"] for item in result["items"] if item.get("price") is not None)
        total = result.get("pagination", {}).get("total")
        priced_count = len(prices)
        if len(prices) >= 2:
            quartiles = statistics.quantiles(prices, n=4, method="inclusive")
            p25, p75 = quartiles[0], quartiles[2]
        elif len(prices) == 1:
            p25 = p75 = prices[0]
        else:
            p25 = p75 = None
        return PriceStats(
            priced_count=priced_count,
            count=priced_count,
            sample_size=len(result["items"]),
            total=total,
            min=prices[0] if prices else None,
            max=prices[-1] if prices else None,
            median=statistics.median(prices) if prices else None,
            mean=statistics.mean(prices) if prices else None,
            p25=p25,
            p75=p75,
            failures=result.get("failures") or [],
        )

    def metadata(self, **filter_kwargs: Any) -> dict[str, Any]:
        static = {
            "sorts": sorted(SORT_PARAMS),
            "good_deal_badges": list(GOOD_DEAL_BADGES),
            "customer_families": list(CUSTOMER_FAMILIES),
            "families": list(VEHICLE_FAMILIES),
            "energies_common": ["ELECTRIC", "DIESEL", "ESSENCE", "HYBRID", "HYBRID_RECHARGEABLE", "GPL"],
            "gearboxes_common": ["AUTO", "MANUAL"],
            "make_model_format": "RENAULT::ZOE",
            "distance_ui_to_api": self._get_distance_buckets(),
            "distance_note": "distance_km / distance_bucket are UI bucket keys, not literal kilometers",
            "page_size": PAGE_SIZE,
            "max_limit": self.max_fetchable_limit(),
            "max_pages_per_search": self.settings.centrale_max_pages_per_search,
        }

        cache_key = urlencode(sorted(self._api_params(**self._normalize_filter_kwargs(filter_kwargs)).items()))
        ttl = max(0.0, float(self.settings.centrale_metadata_cache_ttl))
        now = time.monotonic()
        with self._lock:
            if self._metadata_cache and ttl > 0:
                cached_at, cached = self._metadata_cache
                if now - cached_at <= ttl and cached.get("_cache_key") == cache_key:
                    return {**static, **cached["payload"]}

        facets: dict[str, Any] = {}
        total = None
        try:
            query = self._api_params(**self._normalize_filter_kwargs(filter_kwargs))
            query["aggregations"] = ",".join(AGGREGATION_FACETS)
            response = self._recherche_get(
                f"{RECHERCHE_HOST}/v5/aggregations?{urlencode(query)}",
                prime_query=query,
            )
            body = self._json_body(response)
            if isinstance(body, dict):
                facets = body.get("aggs") if isinstance(body.get("aggs"), dict) else {}
                total = body.get("total")
        except Exception as exc:
            logger.warning("metadata aggregations failed: %s", exc)

        payload = {"facets": facets, "total": total}
        with self._lock:
            self._metadata_cache = (time.monotonic(), {"_cache_key": cache_key, "payload": payload})
        return {**static, **payload}

    def health_status(self) -> dict[str, Any]:
        proxy_count = len(self.settings.proxy_urls())
        upstream_configured = bool(
            self._upstream_api_key_cached or self.settings.centrale_upstream_api_key
        )
        return {
            "status": "up",
            "proxy_configured": proxy_count > 0,
            "proxy_count": proxy_count,
            "primary_strategy": self._primary_strategy(),
            "datadome_configured": bool(self._datadome_client_id),
            "upstream_api_key_configured": upstream_configured,
            "max_fetchable_limit": self.max_fetchable_limit(),
            "listing_js_url": self._listing_js_url or LISTING_JS_FALLBACK,
        }

    def warmup(self) -> dict[str, Any]:
        response = self._session_request(
            "GET",
            f"{CENTRALE_HOST}/",
            json_accept=False,
            api=False,
            use_proxy=self.settings.centrale_www_use_proxy,
            www=True,
        )
        self._sync_cookies(response)
        return {
            "status": response.status_code,
            "has_datadome": bool(self._datadome_client_id),
            "datadome_client_id": bool(self._datadome_client_id),
        }

    def resolve_zip_distance(self, zip_code: str, distance_km: int | None = None) -> dict[str, Any]:
        buckets = self._get_distance_buckets()
        api_distance = None
        if distance_km is not None:
            api_distance = buckets.get(int(distance_km), f"{distance_km}km")
        response = self._session_request(
            "GET",
            f"{GEOLOC_HOST}/v2/zipcodes/{zip_code}",
            json_accept=True,
            api=True,
            use_proxy=True,
            geoloc=True,
        )
        body = self._json_body(response)
        return {
            "zip": zip_code,
            "distance_bucket": distance_km,
            "zip_code_distance": api_distance,
            "geoloc_status": response.status_code,
            "geoloc": body,
        }

    def probe_geoloc(self, zip_code: str) -> dict[str, Any]:
        result = self.resolve_zip_distance(zip_code)
        result["status"] = result.get("geoloc_status")
        return result

    def close(self) -> None:
        with self._lock:
            session = self._recherche_session
            self._recherche_session = None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    def probe_aggregations(self, **params: Any) -> dict[str, Any]:
        query = self._api_params(**self._probe_params(params))
        query["aggregations"] = "REGION,CUSTOMER_FAMILY_CODE"
        response = self._recherche_get(
            f"{RECHERCHE_HOST}/v5/aggregations?{urlencode(query)}",
            prime_query=query,
        )
        return {"status": response.status_code, "body": self._json_body(response)}

    def probe_search_api(self, **params: Any) -> dict[str, Any]:
        query = self._api_params(**self._probe_params(params))
        response = self._recherche_get(
            f"{RECHERCHE_HOST}/v5/search?{urlencode(query)}",
            prime_query=query,
        )
        return {"status": response.status_code, "body": self._json_body(response)}

    def probe_listing_ssr(self, url: str) -> dict[str, Any]:
        safe_url = validate_lacentrale_url(url)
        html = self._request_text(
            safe_url,
            json_accept=False,
            use_proxy=self.settings.centrale_www_use_proxy,
            www=True,
        )
        refs = extract_listing_refs(html)
        return {
            "status": 200 if html else 0,
            "markers": scan_ssr_markers(html),
            "refs": refs,
            "blocked": self._looks_blocked(html),
        }

    def probe_distance_buckets(self) -> dict[int, str]:
        return self._get_distance_buckets()

    def _listing_json(self, ref: str) -> dict[str, Any] | None:
        query = {"references": ref}
        for attempt in range(2):
            try:
                response = self._recherche_get(
                    f"{RECHERCHE_HOST}/v5/search?{urlencode(query)}",
                    prime_query=None,
                )
            except CentraleError:
                if attempt == 0:
                    continue
                return None
            if response.status_code >= 400:
                return None
            body = self._json_body(response)
            if not isinstance(body, dict):
                return None
            hits = body.get("hits")
            if isinstance(hits, list) and hits:
                hit = hits[0]
                item = hit.get("item") if isinstance(hit.get("item"), dict) else hit
                return item if isinstance(item, dict) else None
            if body.get("total") not in (0, None):
                logger.warning(
                    "listing JSON total=%s but hits empty for ref=%s",
                    body.get("total"),
                    ref,
                )
            if body.get("total") in (0, None):
                return None
        return None

    def _listing_html(self, ref: str) -> dict[str, Any] | None:
        item, _error = self._fetch_listing_html_enrichment(ref)
        return item

    def _fetch_listing_html_enrichment(self, ref: str) -> tuple[dict[str, Any] | None, str | None]:
        listing_url = f"{CENTRALE_HOST}/auto-occasion-annonce-{ref}.html"
        try:
            response = self._session_request(
                "GET",
                listing_url,
                json_accept=False,
                use_proxy=self.settings.centrale_www_use_proxy,
                www=True,
            )
        except CentraleError as exc:
            message = str(exc).lower()
            if any(token in message for token in ("403", "429", "blocked")):
                logger.warning("Listing HTML blocked for %s: %s", ref, exc)
                return None, "datadome_blocked"
            logger.warning("Listing HTML request failed for %s: %s", ref, exc)
            return None, "request_failed"

        body = response.text if isinstance(response.text, str) else ""
        if response.status_code in {403, 429} or self._looks_blocked(body):
            logger.warning(
                "Listing HTML blocked for %s (HTTP %s)",
                ref,
                response.status_code,
            )
            return None, "datadome_blocked"

        parsed = self._parse_detail_html(body, ref)
        if parsed:
            return parsed, None
        logger.warning("Listing HTML parse returned no data for %s", ref)
        return None, "parse_failed"

    def _fetch_page_hits(
        self,
        *,
        page: int,
        url: str | None,
        **filter_kwargs: Any,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        strategy = self._primary_strategy()
        errors: list[str] = []

        if strategy in {"json", "auto"}:
            try:
                return self._search_json(page=page, url=url, **filter_kwargs)
            except Exception as exc:
                errors.append(f"json: {exc}")

        if strategy in {"ssr", "auto"}:
            try:
                return self._search_ssr(page=page, url=url, **filter_kwargs)
            except Exception as exc:
                errors.append(f"ssr: {exc}")

        raise CentraleError("; ".join(errors) or "No search strategy available")

    def _ensure_recherche_session(self) -> requests.Session:
        with self._lock:
            if self._recherche_session is not None:
                return self._recherche_session
        proxy = self._next_proxy()
        session = requests.Session(impersonate=random.choice(self.settings.impersonates()))
        session.headers.update(self._headers(json_accept=True, api=True))
        # Deliberately cookie-less: the search API answers 200 to a clean session but 403 as
        # soon as a stale DataDome cookie is replayed, and www.lacentrale.fr never hands out a
        # fresh one anymore (all its HTML is blocked).
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        with self._lock:
            if self._recherche_session is not None:
                try:
                    session.close()
                except Exception:
                    pass
                return self._recherche_session
            self._recherche_session = session
            return session

    def _prime_recherche_query(self, query: dict[str, str]) -> None:
        prime_query = dict(query)
        prime_query.pop("page", None)
        prime_query["aggregations"] = "REGION"
        cache_key = urlencode(sorted(prime_query.items()))
        ttl = max(0.0, min(float(self.settings.centrale_cache_ttl), 60.0))
        now = time.monotonic()
        with self._lock:
            if self._prime_cache and ttl > 0:
                cached_key, cached_at = self._prime_cache
                if cached_key == cache_key and now - cached_at <= ttl:
                    return
        session = self._ensure_recherche_session()
        try:
            response = session.get(
                f"{RECHERCHE_HOST}/v5/aggregations?{urlencode(prime_query)}",
                timeout=self.settings.centrale_timeout,
            )
            if response.status_code < 400:
                self._sync_cookies_from_session(session)
                with self._lock:
                    self._prime_cache = (cache_key, time.monotonic())
        except Exception as exc:
            logger.debug("recherche prime failed: %s", exc)

    def _recherche_get(self, url: str, *, prime_query: dict[str, str] | None = None) -> requests.Response:
        if prime_query is not None:
            self._prime_recherche_query(prime_query)
        session = self._ensure_recherche_session()
        self._throttle()
        response = session.get(url, timeout=self.settings.centrale_timeout, allow_redirects=True)
        if response.status_code in {403, 429}:
            with self._lock:
                self._recherche_session = None
            raise CentraleError(f"search API HTTP {response.status_code}")
        self._sync_cookies_from_session(session)
        return response

    def _sync_cookies_from_session(self, session: requests.Session) -> None:
        with self._lock:
            for name, value in session.cookies.items():
                if name in COOKIE_DENYLIST or name == "datadome":
                    continue
                self._www_cookies[name] = value

    def _search_json(
        self,
        *,
        page: int,
        url: str | None,
        **filter_kwargs: Any,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        if url:
            safe_url = validate_lacentrale_url(url)
            parsed = urlparse(safe_url)
            site_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            query = self._site_params_to_api(site_params, fallback=filter_kwargs, page=page)
        else:
            query = self._api_params(page=page, **filter_kwargs)

        cache_key = f"search:{urlencode(sorted(query.items()))}"
        cached = self._cached_json(cache_key)
        if cached is not None:
            body = cached
        else:
            last_error = None
            body = None
            for attempt in range(self.settings.centrale_max_retries + 1):
                try:
                    response = self._recherche_get(
                        f"{RECHERCHE_HOST}/v5/search?{urlencode(query)}",
                        prime_query=query,
                    )
                    body = self._json_body(response)
                    if response.status_code >= 400:
                        raise CentraleError(f"search API HTTP {response.status_code}")
                    break
                except CentraleError as exc:
                    last_error = exc
                    if attempt >= self.settings.centrale_max_retries:
                        raise
                    time.sleep(min(2**attempt, 4))
            if body is None:
                raise CentraleError(str(last_error) if last_error else "search API failed")
            if not isinstance(body, dict):
                raise CentraleError("search API returned invalid JSON")
            total = body.get("total")
            hits = body.get("hits")
            if isinstance(hits, list) and (hits or total == 0):
                self._store_json(cache_key, body)

        hits = body.get("hits") if isinstance(body, dict) else None
        if not isinstance(hits, list):
            raise CentraleError("search API returned invalid hits")

        metadata = {
            "total": body.get("total") if isinstance(body, dict) else None,
            "seed": body.get("seed") if isinstance(body, dict) else None,
            "page": page,
        }
        return hits, metadata, "json"

    def _search_ssr(
        self,
        *,
        page: int,
        url: str | None,
        **filter_kwargs: Any,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        if url:
            listing_url = validate_lacentrale_url(url)
        else:
            listing_url = self._build_listing_url(page=page, **filter_kwargs)
        listing_url = self._with_page(listing_url, page)
        html = self._request_text(
            listing_url,
            json_accept=False,
            use_proxy=self.settings.centrale_www_use_proxy,
            www=True,
        )
        if self._looks_blocked(html):
            raise CentraleError("listing SSR blocked by DataDome")

        payload = self._parse_listing_html(html)
        hits = self._hits_from_payload(payload, html)
        total = payload.get("total") if isinstance(payload, dict) else None
        seed = payload.get("seed") if isinstance(payload, dict) else None
        metadata = {"total": total, "seed": seed, "page": page}
        return hits, metadata, "ssr"

    def _parse_listing_html(self, html: str) -> dict[str, Any] | None:
        next_data = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if next_data:
            try:
                data = json.loads(next_data.group(1))
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass

        for marker in ("window.__PRELOADED_STATE_LISTING__", "window.__INITIAL_STATE__"):
            index = html.find(marker)
            if index >= 0:
                data = extract_json_object(html, index)
                if data:
                    return data
        return None

    def _hits_from_payload(self, payload: dict[str, Any] | None, html: str) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            for key in ("hits", "classifieds", "listings"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [hit if "item" in hit else {"item": hit} for hit in value if isinstance(hit, dict)]

            nested = payload.get("listing") or payload.get("search") or payload.get("data")
            if isinstance(nested, dict):
                for key in ("hits", "classifieds", "listings"):
                    value = nested.get(key)
                    if isinstance(value, list):
                        return [hit if "item" in hit else {"item": hit} for hit in value if isinstance(hit, dict)]

            props = payload.get("props", {}).get("pageProps", {}) if isinstance(payload.get("props"), dict) else {}
            for key in ("hits", "classifieds", "listings", "searchData"):
                value = props.get(key)
                if isinstance(value, list):
                    return [hit if "item" in hit else {"item": hit} for hit in value if isinstance(hit, dict)]
                if isinstance(value, dict):
                    inner_hits = value.get("hits") or value.get("classifieds")
                    if isinstance(inner_hits, list):
                        return [
                            hit if "item" in hit else {"item": hit}
                            for hit in inner_hits
                            if isinstance(hit, dict)
                        ]

        refs = extract_listing_refs(html)
        if refs:
            return [
                {"item": {"reference": ref, "url": f"{CENTRALE_HOST}/auto-occasion-annonce-{ref}.html"}}
                for ref in refs
            ]
        return []

    def _parse_detail_html(self, html: str, ref: str) -> dict[str, Any] | None:
        item: dict[str, Any] | None = classified_scripts_to_item(html, ref)

        payload = self._parse_listing_html(html)
        if isinstance(payload, dict):
            apollo_ad = find_apollo_ad(payload, ref)
            if apollo_ad:
                item = self._merge_listing_items(apollo_ad_to_item(apollo_ad, ref), item or {})

            if not item or not item.get("price"):
                for key in ("classified", "listing", "vehicle", "item", "ad"):
                    value = payload.get(key)
                    if isinstance(value, dict) and (value.get("reference") or value.get("vehicle")):
                        item = self._merge_listing_items(value, item or {"reference": ref})
                        break
                props = payload.get("props", {}).get("pageProps", {})
                if isinstance(props, dict):
                    for key in ("classified", "listing", "vehicle", "ad"):
                        value = props.get(key)
                        if isinstance(value, dict):
                            item = self._merge_listing_items(value, item or {"reference": ref})
                            break
                    if not item:
                        page_ad = props.get("ad")
                        if isinstance(page_ad, dict):
                            item = self._merge_listing_items(page_ad, item or {"reference": ref})

        if not item:
            item = {"reference": ref}

        if not item.get("title"):
            title_match = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.I)
            if not title_match:
                title_match = re.search(r"<title>([^<]+)</title>", html, re.I)
            if title_match:
                item["title"] = title_match.group(1).strip()

        if item.get("price") is None:
            price_el = re.search(r'class="[^"]*PriceInformation_price__[^"]*"[^>]*>([^<]+)<', html)
            if price_el:
                digits = re.findall(r"\d+", price_el.group(1))
                if digits:
                    item["price"] = int("".join(digits))
            if item.get("price") is None:
                price_match = DETAIL_PRICE_RE.search(html)
                if price_match:
                    item["price"] = int(price_match.group(1) or price_match.group(2))

        if not item.get("description"):
            desc_match = re.search(r'data-test="description"[^>]*>([^<]+)<', html)
            if desc_match:
                item["description"] = desc_match.group(1).strip()

        item.setdefault("reference", ref)
        if item.get("title") or item.get("price") or item.get("description") or item.get("vehicle"):
            return item
        return None

    @staticmethod
    def _merge_listing_items(base: dict[str, Any] | None, override: dict[str, Any] | None) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for source in (base, override):
            if not isinstance(source, dict):
                continue
            for key, value in source.items():
                if value is None:
                    continue
                if key == "vehicle" and isinstance(value, dict):
                    current = merged.get("vehicle")
                    merged["vehicle"] = {**(current if isinstance(current, dict) else {}), **value}
                elif key == "customer" and isinstance(value, dict):
                    current = merged.get("customer")
                    merged["customer"] = {**(current if isinstance(current, dict) else {}), **value}
                elif key == "location" and isinstance(value, dict):
                    current = merged.get("location")
                    merged["location"] = {**(current if isinstance(current, dict) else {}), **value}
                elif key not in merged or merged[key] in (None, "", []):
                    merged[key] = value
        return merged

    def _normalize_hit(
        self,
        hit: dict[str, Any],
        *,
        include_image: bool,
        include_dealer: bool,
        include_vehicle: bool,
        include_description: bool = False,
        raw: bool,
    ) -> SearchResult:
        item = hit.get("item") if isinstance(hit.get("item"), dict) else hit
        vehicle = item.get("vehicle") if isinstance(item.get("vehicle"), dict) else {}
        customer = item.get("customer") if isinstance(item.get("customer"), dict) else {}
        ref = item.get("reference") or item.get("classifiedReference") or item.get("id")
        make = vehicle.get("make") or item.get("make")
        model = vehicle.get("model") or item.get("model")
        version = vehicle.get("version") or vehicle.get("trimLevel") or item.get("version")
        title_parts = [part for part in [make, model, version] if part]
        title = item.get("title") or " ".join(str(part) for part in title_parts) or None
        url = item.get("url") or (f"{CENTRALE_HOST}/auto-occasion-annonce-{ref}.html" if ref else None)
        location = item.get("location") if isinstance(item.get("location"), dict) else None
        if not location:
            location = {
                key: item.get(key)
                for key in ("zipCode", "city", "department", "region", "visitPlace")
                if item.get(key) is not None
            } or None

        energy = vehicle.get("energy") or item.get("energy")
        description_text = item.get("description") if include_description else None
        ev_fields = extract_ev_metadata(
            title=title,
            version=version,
            description=description_text,
            energy=energy,
        )

        return SearchResult(
            id=str(ref) if ref is not None else None,
            title=title,
            make=make,
            model=model,
            version=version,
            year=self._int_value(vehicle.get("year") or item.get("year")),
            mileage=self._int_value(vehicle.get("mileage") or item.get("mileage")),
            energy=energy,
            gearbox=vehicle.get("gearbox") or item.get("gearbox"),
            price=self._float_value(item.get("price")),
            location=location,
            dealer_type=customer.get("family")
            or customer.get("customerFamily")
            or item.get("customerFamily")
            or item.get("customerFamilyCode"),
            good_deal_badge=item.get("goodDealBadge") or item.get("good_deal_badge"),
            url=url,
            image=item.get("photoUrl") or item.get("photo_url") if include_image else None,
            description=description_text,
            features=item.get("features") if include_description else None,
            equipment=item.get("equipment") if include_description else None,
            technical_sheet_url=item.get("technical_sheet_url") if include_description else None,
            dealer=self._compact_customer(customer) if include_dealer else None,
            vehicle=vehicle if include_vehicle and vehicle else None,
            battery_capacity_kwh=ev_fields.get("battery_capacity_kwh"),
            charge_type=ev_fields.get("charge_type"),
            battery_ownership=ev_fields.get("battery_ownership"),
            raw=hit if raw else None,
        )

    @staticmethod
    def _compact_customer(customer: dict[str, Any]) -> dict[str, Any] | None:
        if not customer:
            return None
        keys = ["family", "name", "customerFamily", "customerReference", "zipCode", "city"]
        compact = {key: customer.get(key) for key in keys if customer.get(key) is not None}
        return compact or None

    def _search_filter_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        clean = {key: value for key, value in kwargs.items() if value is not None}
        sort = str(clean.get("sort") or "newest").lower()
        if sort not in SORT_PARAMS:
            raise CentraleError(f"Invalid sort: {sort}")
        clean["sort"] = sort
        return clean

    def _normalize_filter_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, value in kwargs.items():
            if value is None:
                continue
            if key in {"make", "model"}:
                normalized.setdefault("make", kwargs.get("make"))
                normalized.setdefault("model", kwargs.get("model"))
            elif key == "zip":
                normalized["zip_code"] = value
            elif key in {"distance_km", "distance_bucket"}:
                normalized["distance_km"] = value
            else:
                normalized[key] = value
        if normalized.get("make") or normalized.get("model"):
            normalized["makes_models"] = self._make_model(normalized.get("make"), normalized.get("model"))
        return normalized

    def _probe_params(self, params: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(params)
        if "makes_models" not in normalized and (params.get("make") or params.get("model")):
            normalized["makes_models"] = self._make_model(params.get("make"), params.get("model"))
        if "zip_code" not in normalized and params.get("zip"):
            normalized["zip_code"] = params["zip"]
        return normalized

    def _api_params(self, **kwargs: Any) -> dict[str, str]:
        params: dict[str, str] = {
            "page": str(int(kwargs.get("page") or 1) - 1),
            "sortBy": SORT_PARAMS.get(str(kwargs.get("sort") or "newest").lower(), SORT_PARAMS["newest"]),
            "families": str(kwargs.get("families") or "AUTO,UTILITY"),
        }
        makes_models = kwargs.get("makes_models") or self._make_model(kwargs.get("make"), kwargs.get("model"))
        if makes_models:
            params["makesModelsCommercialNames"] = makes_models
        version = kwargs.get("version")
        if version:
            params["version"] = str(version)
        for src, dst in [
            ("price_min", "priceMin"),
            ("price_max", "priceMax"),
            ("year_min", "yearMin"),
            ("year_max", "yearMax"),
            ("mileage_min", "mileageMin"),
            ("mileage_max", "mileageMax"),
        ]:
            value = kwargs.get(src)
            if value is not None:
                params[dst] = str(int(value) if isinstance(value, float) and value.is_integer() else value)

        zip_code = kwargs.get("zip_code") or kwargs.get("zip")
        if zip_code:
            params["zipCode"] = str(zip_code)
            distance = kwargs.get("distance_km")
            if distance is not None:
                buckets = self._get_distance_buckets()
                params["zipCodeDistance"] = buckets.get(int(distance), f"{distance}km")

        for src, dst in [
            ("good_deal", "goodDealBadges"),
            ("customer_family", "customerFamilyCodes"),
            ("energy", "energies"),
            ("gearbox", "gearbox"),
            ("body_type", "categories"),
            ("color", "externalColors"),
            ("internal_color", "internalColors"),
            ("options", "options"),
            ("regions", "regions"),
            ("equipment_level", "equipmentLevel"),
            ("critair", "CRITAIR_MAX"),
            ("max_consumption", "MAX_CONSUMPTION"),
            ("freetext", "freetext"),
        ]:
            value = kwargs.get(src)
            if value is not None:
                params[dst] = str(value)

        if kwargs.get("co2_max") is not None:
            params["co2Max"] = str(kwargs["co2_max"])
        if kwargs.get("doors") is not None:
            params["doors"] = str(kwargs["doors"])
        if kwargs.get("power") is not None:
            params["power"] = str(kwargs["power"])
        if kwargs.get("seats") is not None:
            params["seats"] = str(kwargs["seats"])
        if kwargs.get("four_wheel") is not None:
            params["fourWheel"] = "true" if kwargs["four_wheel"] else "false"

        return params

    def _site_params_to_api(
        self,
        site_params: dict[str, str],
        *,
        fallback: dict[str, Any],
        page: int,
    ) -> dict[str, str]:
        api_params = self._api_params(page=page, **fallback)
        for site_key, api_key in SITE_TO_API_KEYS.items():
            if site_params.get(site_key):
                api_params[api_key] = site_params[site_key]
        if site_params.get("dptCp"):
            api_params["zipCode"] = site_params["dptCp"]
        if site_params.get("distance"):
            buckets = self._get_distance_buckets()
            try:
                api_params["zipCodeDistance"] = buckets.get(int(site_params["distance"]), site_params["distance"])
            except ValueError:
                api_params["zipCodeDistance"] = site_params["distance"]
        if site_params.get("sortBy"):
            api_params["sortBy"] = site_params["sortBy"]
        api_params["page"] = str(page - 1)
        return api_params

    def _build_listing_url(self, *, page: int, **filter_kwargs: Any) -> str:
        params: dict[str, str] = {"families": str(filter_kwargs.get("families") or "AUTO,UTILITY")}
        make_model = self._make_model(filter_kwargs.get("make"), filter_kwargs.get("model"))
        if make_model:
            params["makesModelsCommercialNames"] = make_model
        if filter_kwargs.get("version"):
            params["versions"] = str(filter_kwargs["version"])
        for src, dst in [
            ("price_min", "priceMin"),
            ("price_max", "priceMax"),
            ("year_min", "yearMin"),
            ("year_max", "yearMax"),
            ("mileage_min", "mileageMin"),
            ("mileage_max", "mileageMax"),
        ]:
            value = filter_kwargs.get(src)
            if value is not None:
                params[dst] = str(int(value) if isinstance(value, float) and value.is_integer() else value)
        zip_code = filter_kwargs.get("zip_code") or filter_kwargs.get("zip")
        if zip_code:
            params["dptCp"] = str(zip_code)
        if filter_kwargs.get("distance_km") is not None:
            params["distance"] = str(filter_kwargs["distance_km"])
        for src, dst in [
            ("good_deal", "goodDealBadges"),
            ("customer_family", "customerFamilyCodes"),
            ("energy", "energies"),
            ("gearbox", "gearbox"),
            ("body_type", "categories"),
            ("color", "externalColors"),
            ("internal_color", "internalColors"),
            ("options", "options"),
            ("regions", "regions"),
            ("equipment_level", "equipmentLevel"),
            ("critair", "CRITAIR_MAX"),
            ("max_consumption", "MAX_CONSUMPTION"),
            ("freetext", "freetext"),
        ]:
            value = filter_kwargs.get(src)
            if value is not None:
                params[dst] = str(value)
        if filter_kwargs.get("co2_max") is not None:
            params["co2Max"] = str(filter_kwargs["co2_max"])
        if filter_kwargs.get("doors") is not None:
            params["doors"] = str(filter_kwargs["doors"])
        if filter_kwargs.get("power") is not None:
            params["power"] = str(filter_kwargs["power"])
        if filter_kwargs.get("seats") is not None:
            params["seats"] = str(filter_kwargs["seats"])
        if filter_kwargs.get("four_wheel") is not None:
            params["fourWheel"] = "true" if filter_kwargs["four_wheel"] else "false"
        if page > 1:
            params["page"] = str(page)
        sort = str(filter_kwargs.get("sort") or "newest").lower()
        if sort in SORT_PARAMS and sort != "newest":
            params["sortBy"] = SORT_PARAMS[sort]
        return f"{CENTRALE_HOST}/listing?{urlencode(params)}"

    @staticmethod
    def _make_model(make: str | None, model: str | None) -> str | None:
        if not make and not model:
            return None
        if make and model:
            return f"{make.strip().upper()}::{model.strip().upper()}"
        if make and not model:
            return f"{make.strip().upper()}::"
        return f"::{model.strip().upper()}"

    @staticmethod
    def _with_page(url: str, page: int) -> str:
        parsed = urlparse(url)
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if page > 1:
            params["page"] = str(page)
        else:
            params.pop("page", None)
        return urlunparse(parsed._replace(query=urlencode(params)))

    def _primary_strategy(self) -> str:
        value = (self.settings.centrale_primary_strategy or "auto").lower()
        if value in {"json", "ssr"}:
            return value
        return "auto"

    def _discover_listing_js_url(self) -> str:
        return self._discover_listing_js_urls()[0]

    def _discover_listing_js_urls(self) -> list[str]:
        with self._lock:
            if self._listing_js_url:
                return [self._listing_js_url, LISTING_JS_FALLBACK]
        discovered: list[str] = []
        try:
            html = self._request_text(
                f"{CENTRALE_HOST}/listing",
                json_accept=False,
                use_proxy=self.settings.centrale_www_use_proxy,
                www=True,
            )
            discovered = discover_listing_js_urls(html)
        except Exception as exc:
            logger.debug("listing JS discovery failed: %s", exc)
        if not discovered:
            discovered = [LISTING_JS_FALLBACK]
        with self._lock:
            if not self._listing_js_url:
                self._listing_js_url = discovered[0]
            return discovered

    def _get_distance_buckets(self) -> dict[int, str]:
        with self._lock:
            if self._distance_buckets is not None:
                return self._distance_buckets
        try:
            js = self._request_text(self._discover_listing_js_url(), json_accept=False, use_proxy=True)
            found = {int(match.group(1).rstrip("km")): match.group(1) for match in ZIP_DISTANCE_RE.finditer(js)}
            buckets = {**DISTANCE_UI_TO_API, **found} if found else DISTANCE_UI_TO_API
        except Exception:
            buckets = DISTANCE_UI_TO_API
        with self._lock:
            self._distance_buckets = buckets
            return buckets

    def _resolve_upstream_api_key(self) -> str:
        with self._lock:
            if self._upstream_api_key_cached:
                return self._upstream_api_key_cached
            if self.settings.centrale_upstream_api_key:
                self._upstream_api_key_cached = self.settings.centrale_upstream_api_key
                return self._upstream_api_key_cached
        key = extract_upstream_api_key(self)
        with self._lock:
            self._upstream_api_key_cached = key
            return key

    def _request_text(
        self,
        url: str,
        *,
        json_accept: bool,
        use_proxy: bool | None = None,
        www: bool = False,
        geoloc: bool = False,
    ) -> str:
        response = self._session_request(
            "GET",
            url,
            json_accept=json_accept,
            api="recherche.lacentrale.fr" in url or geoloc,
            use_proxy=use_proxy,
            www=www,
            geoloc=geoloc,
        )
        if response.status_code >= 400:
            raise CentraleError(f"HTTP {response.status_code} for {url}")
        return response.text

    def _session_request(
        self,
        method: str,
        url: str,
        *,
        json_accept: bool,
        api: bool = False,
        use_proxy: bool | None = None,
        www: bool = False,
        geoloc: bool = False,
    ) -> requests.Response:
        if method.upper() != "GET":
            raise CentraleError(f"Unsupported HTTP method: {method}")
        last_error = None
        for attempt in range(self.settings.centrale_max_retries + 1):
            self._throttle()
            proxy = None
            if use_proxy is True:
                proxy = self._next_proxy()
            elif use_proxy is not False and use_proxy is None and (
                "recherche.lacentrale.fr" in url or geoloc
            ):
                proxy = self._next_proxy()
            impersonate = random.choice(self.settings.impersonates())
            session = requests.Session(impersonate=impersonate)
            session.headers.update(
                self._headers(json_accept=json_accept, api=api, www=www, geoloc=geoloc)
            )
            with self._lock:
                if self._www_cookies:
                    session.cookies.update(self._www_cookies)
            if proxy:
                session.proxies = {"http": proxy, "https": proxy}
            try:
                response = session.get(url, timeout=self.settings.centrale_timeout, allow_redirects=True)
                self._sync_cookies(response)
                body_preview = response.text[:50000] if isinstance(response.text, str) else ""
                if response.status_code in {403, 429} or self._looks_blocked(body_preview):
                    last_error = f"blocked: HTTP {response.status_code}"
                    time.sleep(min(2**attempt, 8))
                    continue
                if response.status_code >= 400 and response.status_code != 404:
                    last_error = f"HTTP {response.status_code}"
                    time.sleep(min(2**attempt, 8))
                    continue
                return response
            except CentraleError:
                raise
            except Exception as exc:
                last_error = str(exc)
                time.sleep(min(2**attempt, 8))
            finally:
                session.close()
        raise CentraleError(f"Request failed after retries: {last_error}")

    def _headers(self, *, json_accept: bool, api: bool, www: bool = False, geoloc: bool = False) -> dict[str, str]:
        headers = {
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": f"{CENTRALE_HOST}/",
        }
        if json_accept:
            headers["Accept"] = "application/json, text/plain, */*"
        else:
            headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            headers["Upgrade-Insecure-Requests"] = "1"
        if api or geoloc:
            headers["X-Client-Source"] = self.settings.centrale_client_source
            api_key = self._resolve_upstream_api_key()
            if api_key and (api or geoloc):
                headers["x-api-key"] = api_key
        if (www or geoloc) and self._datadome_client_id:
            headers["x-datadome-clientid"] = self._datadome_client_id
        return headers

    def _sync_cookies(self, response: requests.Response) -> None:
        with self._lock:
            for name, value in response.cookies.items():
                if name in COOKIE_DENYLIST:
                    continue
                if name == "datadome":
                    # Kept in memory only: a replayed DataDome cookie is a guaranteed 403.
                    self._datadome_client_id = value
                    continue
                self._www_cookies[name] = value
            self._save_cookies_unlocked()

    @staticmethod
    def _json_body(response: requests.Response) -> Any:
        try:
            return response.json()
        except Exception:
            return None

    @staticmethod
    def _looks_blocked(body: str) -> bool:
        lower = body[:50000].lower()
        if "captcha-delivery.com" in lower or "geo.captcha-delivery.com" in lower:
            return True
        if "please enable js and disable any ad blocker" in lower:
            return True
        if "var dd=" in lower and ("captcha" in lower or '"rt":"c"' in lower):
            return True
        if "datadome" in lower and ("captcha" in lower or "blocked" in lower):
            if "__next_data__" in lower:
                return False
            return True
        return False

    def _throttle(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last_request
            minimum = self.settings.centrale_min_interval
            wait_for = random.uniform(minimum, minimum * 1.5) - elapsed
            if wait_for > 0:
                time.sleep(wait_for)
            self._last_request = time.monotonic()

    def _next_proxy(self) -> str | None:
        with self._lock:
            return self._next_proxy_unlocked()

    def _next_proxy_unlocked(self) -> str | None:
        proxies = self.settings.proxy_urls()
        if not proxies:
            return None
        if self.settings.centrale_rotate_proxy_per_request:
            proxy = proxies[self._proxy_index % len(proxies)]
            self._proxy_index += 1
            return proxy
        return proxies[0]

    def _load_cookies(self) -> None:
        path = Path(self.settings.centrale_cookie_file)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            with self._lock:
                self._www_cookies = {
                    str(key): str(value)
                    for key, value in data.items()
                    if str(key) not in COOKIE_DENYLIST and str(key) != "datadome"
                }

    def _save_cookies_unlocked(self) -> None:
        if not self._www_cookies:
            return
        path = Path(self.settings.centrale_cookie_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._www_cookies, indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _cached_json(self, key: str) -> dict[str, Any] | None:
        ttl = max(0.0, float(self.settings.centrale_cache_ttl))
        if ttl <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            cached = self._json_cache.get(key)
            if not cached:
                return None
            cached_at, data = cached
            if now - cached_at > ttl:
                self._json_cache.pop(key, None)
                return None
            return data

    def _store_json(self, key: str, data: dict[str, Any]) -> None:
        max_entries = max(0, int(self.settings.centrale_cache_max_entries))
        if self.settings.centrale_cache_ttl <= 0 or max_entries <= 0:
            return
        with self._lock:
            if len(self._json_cache) >= max_entries:
                oldest = min(self._json_cache, key=lambda item: self._json_cache[item][0])
                self._json_cache.pop(oldest, None)
            self._json_cache[key] = (time.monotonic(), data)

    @staticmethod
    def _listing_cache_key(ref: str, **flags: bool) -> str:
        parts = [ref] + [f"{name}={int(value)}" for name, value in sorted(flags.items())]
        return "listing:" + ":".join(parts)

    def _cached_listing(self, key: str) -> dict[str, Any] | None:
        ttl = max(0.0, float(self.settings.centrale_listing_cache_ttl))
        if ttl <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            cached = self._listing_cache.get(key)
            if not cached:
                return None
            cached_at, data = cached
            if now - cached_at > ttl:
                self._listing_cache.pop(key, None)
                return None
            return data

    def _store_listing(self, key: str, data: dict[str, Any]) -> None:
        max_entries = max(0, int(self.settings.centrale_listing_cache_max_entries))
        ttl = max(0.0, float(self.settings.centrale_listing_cache_ttl))
        if ttl <= 0 or max_entries <= 0:
            return
        with self._lock:
            if len(self._listing_cache) >= max_entries:
                oldest = min(self._listing_cache, key=lambda item: self._listing_cache[item][0])
                self._listing_cache.pop(oldest, None)
            self._listing_cache[key] = (time.monotonic(), data)

    @staticmethod
    def _float_value(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int_value(value: Any) -> int | None:
        try:
            return int(float(value)) if value is not None else None
        except (TypeError, ValueError):
            return None


def extract_upstream_api_key(client: "CentraleClient") -> str:
    if client.settings.centrale_upstream_api_key:
        return client.settings.centrale_upstream_api_key

    errors: list[str] = []
    candidate_urls: list[str] = []
    try:
        listing_html = client._request_text(
            f"{CENTRALE_HOST}/listing",
            json_accept=False,
            use_proxy=client.settings.centrale_www_use_proxy,
            www=True,
        )
        candidate_urls.extend(discover_listing_js_urls(listing_html))
    except Exception as exc:
        errors.append(f"listing html: {exc}")

    if not candidate_urls:
        candidate_urls.extend(client._discover_listing_js_urls())

    seen_urls: set[str] = set()
    for js_url in candidate_urls:
        if js_url in seen_urls:
            continue
        seen_urls.add(js_url)
        try:
            js_source = client._request_text(js_url, json_accept=False, use_proxy=True)
            key = extract_api_key_from_js(js_source)
            if key:
                logger.info("Upstream API key discovered from %s", js_url)
                return key
        except Exception as exc:
            errors.append(f"{js_url}: {exc}")

    for fallback_key in (*client.settings.upstream_api_key_fallbacks(), *UPSTREAM_API_KEY_FALLBACKS):
        if fallback_key:
            logger.warning("Using configured fallback upstream API key")
            return fallback_key

    detail = "; ".join(errors) if errors else "no JS bundle matched"
    raise CentraleError(f"Unable to extract CENTRALE_UPSTREAM_API_KEY from listing JS ({detail})")


LISTING_JS_URL = LISTING_JS_FALLBACK
