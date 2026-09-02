import logging
import math
import random
import re
import statistics
import threading
import time
import unicodedata
import uuid
from typing import Any
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlparse, urlunparse

from curl_cffi import requests

from app.config import Settings
from app.models import PriceStats, SearchResult

try:
    import lbc
    from lbc.utils import build_search_payload_with_url
except Exception:  # pragma: no cover - the service can still run with numeric categories.
    lbc = None
    build_search_payload_with_url = None


logger = logging.getLogger(__name__)

LEBONCOIN_HOST = "https://www.leboncoin.fr"
LBC_API_HOST = "https://api.leboncoin.fr"
SEARCH_ENDPOINT = f"{LBC_API_HOST}/finder/search"
AD_ENDPOINT = f"{LBC_API_HOST}/api/adfinder/v1/classified/{{ad_id}}"
FRONTEND_PAGE_SIZE = 35

CATEGORY_ALIASES = {
    "mode": "72",
    "fashion": "72",
    "vetements": "22",
    "vêtements": "22",
    "chaussures": "53",
    "sneakers": "53",
    "basket": "53",
    "baskets": "53",
    "accessoires": "47",
    "bagagerie": "47",
    "montres": "42",
    "bijoux": "42",
    "electronique": "14",
    "telephone": "17",
    "telephones": "17",
    "jeux_video": "84",
    "velos": "55",
    "velo": "55",
}

SORT_PARAMS = {
    "newest": {"sort": "time", "order": "desc"},
    "recent": {"sort": "time", "order": "desc"},
    "time_desc": {"sort": "time", "order": "desc"},
    "oldest": {"sort": "time", "order": "asc"},
    "time_asc": {"sort": "time", "order": "asc"},
    "price_low": {"sort": "price", "order": "asc"},
    "price_asc": {"sort": "price", "order": "asc"},
    "price_high": {"sort": "price", "order": "desc"},
    "price_desc": {"sort": "price", "order": "desc"},
    "relevance": {"sort": "relevance"},
}


class LeboncoinError(RuntimeError):
    pass


class LeboncoinClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        # Pacing gets its own lock: `_lock` guards the caches and the shared session,
        # and a cache hit must not queue behind another request's throttle sleep.
        self._throttle_lock = threading.Lock()
        self._last_request = 0.0
        self._proxy_index = 0
        self._next_data_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._session: Any = None
        self._session_proxy: str | None = None
        self._session_born = 0.0

    def search(
        self,
        *,
        text: str | None = None,
        category: str | None = None,
        location: str | None = None,
        lat: float | None = None,
        lng: float | None = None,
        radius: int | None = None,
        price_min: float | None = None,
        price_max: float | None = None,
        sort: str = "newest",
        page: int = 1,
        limit: int = 20,
        url: str | None = None,
        include_image: bool = False,
        include_images: bool = False,
        include_body: bool = False,
        include_owner: bool = False,
        include_attributes: bool = False,
        include_coordinates: bool = False,
        debug: bool = False,
        raw: bool = False,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, FRONTEND_PAGE_SIZE * self.settings.lbc_max_pages_per_search))
        pages_to_fetch = max(1, min(math.ceil(limit / FRONTEND_PAGE_SIZE), self.settings.lbc_max_pages_per_search))

        ads: list[dict[str, Any]] = []
        failures: list[str] = []
        metadata: dict[str, Any] = {}
        source_pages: list[int] = []

        for page_number in range(page, page + pages_to_fetch):
            search_url = self._build_search_url(
                text=text,
                category=category,
                location=location,
                lat=lat,
                lng=lng,
                radius=radius,
                price_min=price_min,
                price_max=price_max,
                sort=sort,
                page=page_number,
                url=url,
            )
            try:
                page_size = max(1, min(limit - len(ads), FRONTEND_PAGE_SIZE))
                search_data = self._fetch_search_json(search_url, page_number, page_size)
                if not isinstance(search_data, dict):
                    raise LeboncoinError("Leboncoin API did not return search data")
                page_ads = search_data.get("ads") or []
                if not isinstance(page_ads, list):
                    raise LeboncoinError("Leboncoin searchData.ads is not a list")
                ads.extend(ad for ad in page_ads if isinstance(ad, dict))
                source_pages.append(page_number)
                if not metadata:
                    metadata = {
                        "total": search_data.get("total"),
                        "max_pages": search_data.get("max_pages"),
                    }
                    if debug:
                        metadata.update(
                            {
                                "source_pages": source_pages,
                                "total_all": search_data.get("total_all"),
                                "total_private": search_data.get("total_private"),
                                "total_pro": search_data.get("total_pro"),
                                "total_shippable": search_data.get("total_shippable"),
                            }
                        )
            except Exception as exc:
                failures.append(f"page {page_number}: {exc}")
                if page_number == page and not ads:
                    break

        if not ads and failures:
            raise LeboncoinError("; ".join(failures))

        items = [
            self._normalize_ad(
                ad,
                include_image=include_image,
                include_images=include_images,
                include_body=include_body,
                include_owner=include_owner,
                include_attributes=include_attributes,
                include_coordinates=include_coordinates,
                include_raw=raw,
            ).model_dump(exclude_none=True)
            for ad in ads[:limit]
        ]
        data = {
            "items": items,
            "pagination": {
                "page": page,
                "limit": limit,
                "returned": len(items),
                **metadata,
            },
        }
        if debug:
            data["source"] = "finder_api"
        if failures:
            data["failures"] = failures
        return data

    def ad(
        self,
        ad_id: int | str,
        *,
        include_image: bool = False,
        include_images: bool = False,
        include_body: bool = True,
        include_owner: bool = False,
        include_attributes: bool = False,
        include_coordinates: bool = False,
        debug: bool = False,
        raw: bool = False,
    ) -> dict[str, Any]:
        clean_id = str(ad_id).strip()
        ad = self._fetch_ad_json(clean_id)
        if not isinstance(ad, dict):
            raise LeboncoinError(f"Unable to find ad {clean_id} in Leboncoin API data")
        normalized = self._normalize_ad(
            ad,
            include_image=include_image,
            include_images=include_images,
            include_body=include_body,
            include_owner=include_owner,
            include_attributes=include_attributes,
            include_coordinates=include_coordinates,
            include_raw=raw,
        ).model_dump(exclude_none=True)
        data: dict[str, Any] = {"item": normalized}
        if debug:
            data.update({"source": "finder_api", "details_available": True})
        return data

    def price_stats(self, **kwargs: Any) -> PriceStats:
        kwargs["limit"] = min(max(int(kwargs.get("limit") or 70), 1), FRONTEND_PAGE_SIZE * self.settings.lbc_max_pages_per_search)
        result = self.search(**kwargs)
        prices = [item["price"] for item in result["items"] if item.get("price") is not None]
        return PriceStats(
            count=len(prices),
            min=min(prices) if prices else None,
            max=max(prices) if prices else None,
            median=statistics.median(prices) if prices else None,
            sample_size=len(result["items"]),
            failures=result.get("failures") or [],
        )

    def metadata(self) -> dict[str, Any]:
        categories = []
        if lbc:
            for category in lbc.Category:
                key, path = self._category_info(category.value)
                categories.append(
                    {
                        "id": str(category.value),
                        "key": key or category.name.lower(),
                        "name": path[-1] if path else self._humanize_category_part(category.name),
                        "path": path,
                    }
                )
        return {
            "categories": categories,
            "category_aliases": CATEGORY_ALIASES,
            "sorts": sorted(SORT_PARAMS),
            "location_formats": ["dept:75", "region:11", "paris", "ile_de_france", "d_75", "r_11", "48.8566,2.3522,10000"],
            "enriched_fields": [
                "condition",
                "brand",
                "old_price",
                "image_count",
                "shipping",
                "seller_rating",
                "options",
                "category_key",
                "category_path",
                "attributes_map when include_attributes=true",
            ],
        }

    def _fetch_search_json(self, search_url: str, page_number: int, page_size: int = FRONTEND_PAGE_SIZE) -> dict[str, Any]:
        page_size = max(1, min(int(page_size), FRONTEND_PAGE_SIZE))
        cache_key = f"{search_url}|{page_size}"
        cached = self._cached_json(cache_key)
        if cached is not None:
            return cached
        if build_search_payload_with_url is None:
            raise LeboncoinError("lbc package is not available to build search payloads")
        payload = build_search_payload_with_url(url=search_url, limit=page_size, page=page_number)
        self._sanitize_payload(payload)
        # Keep the web page semantics (a page is always FRONTEND_PAGE_SIZE ads wide) while
        # only downloading the ads we actually return: saves proxy bandwidth on small limits.
        payload["offset"] = FRONTEND_PAGE_SIZE * (page_number - 1)
        payload["limit_alu"] = 0  # drop sponsored "a la une" ads, they are never returned
        payload["disable_total"] = False
        payload.setdefault("filters", {}).setdefault("enums", {}).setdefault("ad_type", ["offer"])
        data = self._request_json("POST", SEARCH_ENDPOINT, payload=payload)
        self._store_json(cache_key, data)
        return data

    @staticmethod
    def _sanitize_payload(payload: dict[str, Any]) -> None:
        """Work around two bugs in lbc.utils.build_search_payload_with_url.

        It never percent-decodes query values (so `text=nike+tn` is searched literally), and
        its `page` guard tests the value instead of the key, turning `&page=2` into a bogus
        `filters.enums.page` that makes the API return zero ads.
        """
        filters = payload.get("filters")
        if not isinstance(filters, dict):
            return
        enums = filters.get("enums")
        if isinstance(enums, dict):
            enums.pop("page", None)
            for key, values in list(enums.items()):
                if isinstance(values, list):
                    enums[key] = [unquote_plus(str(value)) for value in values]
            if not enums:
                filters.pop("enums", None)
        keywords = filters.get("keywords")
        if isinstance(keywords, dict) and isinstance(keywords.get("text"), str):
            keywords["text"] = unquote_plus(keywords["text"])

    def _fetch_ad_json(self, ad_id: str) -> dict[str, Any]:
        cache_key = f"ad:{ad_id}"
        cached = self._cached_json(cache_key)
        if cached is not None:
            return cached
        data = self._request_json("GET", AD_ENDPOINT.format(ad_id=ad_id), needs_cookie=True)
        self._store_json(cache_key, data)
        return data

    def _cached_json(self, key: str) -> dict[str, Any] | None:
        ttl = max(0.0, float(self.settings.lbc_cache_ttl))
        if ttl <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            cached = self._next_data_cache.get(key)
            if not cached:
                return None
            cached_at, data = cached
            if now - cached_at > ttl:
                self._next_data_cache.pop(key, None)
                return None
            return data

    def _store_json(self, key: str, data: dict[str, Any]) -> None:
        max_entries = max(0, int(self.settings.lbc_cache_max_entries))
        if self.settings.lbc_cache_ttl <= 0 or max_entries <= 0:
            return
        with self._lock:
            if len(self._next_data_cache) >= max_entries:
                oldest_key = min(self._next_data_cache, key=lambda key: self._next_data_cache[key][0])
                self._next_data_cache.pop(oldest_key, None)
            self._next_data_cache[key] = (time.monotonic(), data)

    def _new_session(self, proxy: str | None) -> Any:
        impersonate = random.choice(self.settings.impersonates())
        session = requests.Session(impersonate=impersonate)
        session.headers.update(self._headers(impersonate))
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        return session

    def _acquire_session(self, proxy: str | None) -> Any:
        """Reuse a warm session (and its DataDome cookie) instead of re-handshaking every call.

        Keeping the cookie jar alive is the main bandwidth saver: a fresh session has to be
        primed before the ad endpoint accepts it, and each prime costs an extra round trip.
        """
        ttl = max(0.0, float(self.settings.lbc_session_ttl))
        with self._lock:
            fresh_enough = ttl > 0 and (time.monotonic() - self._session_born) < ttl
            if self._session is not None and self._session_proxy == proxy and fresh_enough:
                return self._session
            self._drop_session_locked()
            self._session = self._new_session(proxy)
            self._session_proxy = proxy
            self._session_born = time.monotonic()
            return self._session

    def _drop_session_locked(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
        self._session = None
        self._session_proxy = None
        self._session_born = 0.0

    def _drop_session(self) -> None:
        with self._lock:
            self._drop_session_locked()

    def close(self) -> None:
        """Release the warm session and its connection pool on shutdown."""
        with self._lock:
            self._drop_session_locked()
            self._next_data_cache.clear()

    def _prime_session(self, session: Any) -> None:
        """Mint a DataDome cookie with the cheapest request that produces one.

        The homepage works but weighs ~400KB; a 1-result search POST is ~8KB and mints the
        same cookie, so we use that instead.
        """
        if session.cookies.get("datadome"):
            return
        session.post(
            SEARCH_ENDPOINT,
            json={
                "filters": {},
                "limit": 1,
                "limit_alu": 0,
                "offset": 0,
                "disable_total": True,
                "extend": False,
                "listing_source": "direct-search",
            },
            timeout=self.settings.lbc_timeout,
        )

    def _request_json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        needs_cookie: bool = False,
    ) -> dict[str, Any]:
        last_error = None
        candidates = self._proxy_candidates()
        for attempt in range(self.settings.lbc_max_retries + 1):
            self._throttle()
            proxy = candidates[attempt % len(candidates)]
            session = self._acquire_session(proxy)
            try:
                if needs_cookie:
                    self._prime_session(session)
                started = time.monotonic()
                response = session.request(method, url, json=payload, timeout=self.settings.lbc_timeout)
                elapsed = time.monotonic() - started
                if elapsed > self.settings.lbc_slow_session_seconds:
                    # The session is pinned to one residential exit IP for its whole life, so a
                    # slow answer means every later call would be slow too. Retire it and let the
                    # next request draw a new IP; the current response is still usable.
                    self._drop_session()
                if response.status_code == 403:
                    # A burnt residential exit IP, not a rate limit: the next connection gets a
                    # different one, so reconnect immediately instead of backing off.
                    last_error = "blocked: HTTP 403"
                    self._drop_session()
                elif response.status_code == 429:
                    last_error = "rate limited: HTTP 429"
                    self._drop_session()
                    time.sleep(min(2**attempt, 8))
                elif response.status_code in {404, 410}:
                    raise LeboncoinError("Leboncoin resource not found")
                elif response.status_code >= 400:
                    last_error = f"HTTP {response.status_code}"
                    time.sleep(min(2**attempt, 8))
                else:
                    try:
                        return response.json()
                    except Exception as exc:
                        last_error = f"invalid JSON response: {exc}"
                        time.sleep(min(2**attempt, 8))
            except LeboncoinError:
                raise
            except Exception as exc:
                # Usually a tarpitted exit IP timing out; a fresh connection re-rolls it.
                last_error = f"{type(exc).__name__}: {exc}"
                self._drop_session()
            logger.warning(
                "leboncoin attempt %s/%s failed via %s: %s",
                attempt + 1,
                self.settings.lbc_max_retries + 1,
                "proxy" if proxy else "direct",
                last_error,
            )
        raise LeboncoinError(f"Leboncoin request failed after retries: {last_error}")

    def _throttle(self) -> None:
        """Space out upstream calls without blocking cache readers.

        The sleep is held under the dedicated throttle lock so concurrent callers
        still queue (that is the point of the pacing), while `_lock` stays free for
        cache and session bookkeeping.
        """
        with self._throttle_lock:
            elapsed = time.monotonic() - self._last_request
            wait_for = self.settings.lbc_min_interval - elapsed
            if wait_for > 0:
                time.sleep(wait_for)
            self._last_request = time.monotonic()

    def _proxy_candidates(self) -> list[str | None]:
        # Always route through the residential pool: it keeps the server's own IP out of
        # DataDome's reputation table. Some exit IPs are burnt (403) or bandwidth-throttled,
        # hence the retry loop walking the pool. Direct access is only used when no proxy is
        # configured at all, or when LBC_ALLOW_DIRECT_FALLBACK is enabled as a last resort.
        candidates: list[str | None] = list(self.settings.proxy_urls())
        if not candidates:
            return [None]
        if self.settings.lbc_rotate_proxy_per_request and len(candidates) > 1:
            # Start each request at the next pool entry, otherwise a healthy first proxy
            # would serve every single call and the rest of the pool would stay idle.
            with self._lock:
                offset = self._proxy_index % len(candidates)
                self._proxy_index += 1
            candidates = candidates[offset:] + candidates[:offset]
        if self.settings.lbc_allow_direct_fallback:
            candidates.append(None)
        return candidates

    @staticmethod
    def _headers(impersonate: str = "safari_ios") -> dict[str, str]:
        return {
            "User-Agent": LeboncoinClient._mobile_user_agent(impersonate),
            "Accept": "application/json",
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Content-Type": "application/json",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }

    @staticmethod
    def _mobile_user_agent(impersonate: str = "safari_ios") -> str:
        # Keep the announced OS consistent with the TLS fingerprint we impersonate.
        if "ios" in impersonate or impersonate.startswith("safari"):
            os_version = random.choice(["18.4", "18.5", "18.6", "26.0", "26.1"])
            return f"LBC;iOS;{os_version};iPhone;phone;{uuid.uuid4()};wifi;101.44.0"
        os_version = random.choice(["12", "13", "14", "15"])
        model = random.choice(["Pixel 7", "Pixel 8", "SM-S918B", "SM-A546B"])
        return f"LBC;Android;{os_version};{model};phone;{uuid.uuid4().hex[:16]};wifi;100.85.2"

    def _build_search_url(
        self,
        *,
        text: str | None,
        category: str | None,
        location: str | None,
        lat: float | None,
        lng: float | None,
        radius: int | None,
        price_min: float | None,
        price_max: float | None,
        sort: str,
        page: int,
        url: str | None,
    ) -> str:
        if url:
            return self._with_page(url, page)

        params: dict[str, str] = {}
        if text:
            params["text"] = text
        category_id = self._category_id(category)
        if category_id:
            params["category"] = category_id
        location_value = self._location_value(location=location, lat=lat, lng=lng, radius=radius)
        if location_value:
            params["locations"] = location_value
        price_value = self._range_value(price_min, price_max)
        if price_value:
            params["price"] = price_value
        params.update(SORT_PARAMS.get(self._key(sort), SORT_PARAMS["newest"]))
        if page > 1:
            params["page"] = str(page)
        return f"{LEBONCOIN_HOST}/recherche?{urlencode(params)}"

    @staticmethod
    def _with_page(url: str, page: int) -> str:
        parsed = urlparse(url if url.startswith("http") else f"{LEBONCOIN_HOST}{url}")
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if page > 1:
            params["page"] = str(page)
        else:
            params.pop("page", None)
        return urlunparse(parsed._replace(query=urlencode(params)))

    def _category_id(self, category: str | None) -> str | None:
        if not category:
            return None
        clean = str(category).strip()
        if clean.isdigit():
            return clean
        key = self._key(clean)
        if key in CATEGORY_ALIASES:
            return CATEGORY_ALIASES[key]
        if lbc:
            for item in lbc.Category:
                if self._key(item.name) == key:
                    return item.value
        raise LeboncoinError(f"Unknown category: {category}")

    def _location_value(
        self,
        *,
        location: str | None,
        lat: float | None,
        lng: float | None,
        radius: int | None,
    ) -> str | None:
        if lat is not None and lng is not None:
            safe_radius = int(radius or 10000)
            return f"p_api__{lat}_{lng}_{safe_radius}_{safe_radius}"
        if not location:
            return None

        clean = location.strip()
        lowered = clean.lower()
        if "__" in clean or lowered.startswith(("d_", "r_", "p_")):
            return clean
        if "," in clean:
            parts = [part.strip() for part in clean.split(",")]
            if len(parts) >= 2:
                safe_radius = int(float(parts[2])) if len(parts) >= 3 and parts[2] else 10000
                return f"p_api__{float(parts[0])}_{float(parts[1])}_{safe_radius}_{safe_radius}"
        for prefix in ["dept:", "department:", "departement:", "département:"]:
            if lowered.startswith(prefix):
                return f"d_{int(clean.split(':', 1)[1])}"
        if lowered.startswith("region:") or lowered.startswith("région:"):
            return f"r_{int(clean.split(':', 1)[1])}"
        if clean.isdigit():
            return f"d_{int(clean)}"

        key = self._key(clean)
        if lbc:
            for department in lbc.Department:
                if self._key(department.name) == key:
                    return f"d_{department.value[2]}"
            for region in lbc.Region:
                if self._key(region.name) == key:
                    return f"r_{region.value[0]}"
        raise LeboncoinError(f"Unknown location: {location}")

    @staticmethod
    def _range_value(minimum: float | None, maximum: float | None) -> str | None:
        if minimum is None and maximum is None:
            return None
        left_value = 0 if minimum is None and maximum is not None else minimum
        left = "" if left_value is None else str(int(left_value) if float(left_value).is_integer() else left_value)
        right = "" if maximum is None else str(int(maximum) if float(maximum).is_integer() else maximum)
        return f"{left}-{right}"

    def _normalize_ad(
        self,
        ad: dict[str, Any],
        *,
        include_image: bool,
        include_images: bool,
        include_body: bool,
        include_owner: bool,
        include_attributes: bool,
        include_coordinates: bool,
        include_raw: bool,
    ) -> SearchResult:
        ad_id = ad.get("list_id")
        url = self._absolute_url(ad.get("url"))
        images = self._images(ad)
        attributes = ad.get("attributes")
        attribute_lookup = self._attribute_lookup(attributes)
        category_key, category_path = self._category_info(ad.get("category_id"))
        return SearchResult(
            id=ad_id,
            title=ad.get("subject"),
            body=ad.get("body") if include_body else None,
            brand=self._brand(ad.get("brand"), attribute_lookup),
            category_id=str(ad.get("category_id")) if ad.get("category_id") is not None else None,
            category_name=ad.get("category_name"),
            category_key=category_key,
            category_path=category_path,
            ad_type=ad.get("ad_type"),
            status=ad.get("status"),
            condition=self._attribute_value(attribute_lookup, "condition"),
            price=self._price(ad),
            old_price=self._float_value(self._attribute_value(attribute_lookup, "old_price")),
            currency="EUR",
            url=url,
            image=images[0] if include_image and images else None,
            image_count=self._image_count(ad, images),
            # `or None` so an ad without photos omits the key instead of shipping
            # `"images": []` to the model for no information.
            images=(images or None) if include_images else None,
            first_publication_date=ad.get("first_publication_date"),
            index_date=ad.get("index_date"),
            location=self._compact_location(ad.get("location"), include_coordinates=include_coordinates),
            owner=self._compact_owner(ad.get("owner")) if include_owner else None,
            seller_rating=self._seller_rating(attribute_lookup),
            shipping=self._shipping(attribute_lookup),
            options=self._compact_options(ad.get("options"), ad.get("is_boosted")),
            attributes=self._compact_attributes(attributes) if include_attributes else None,
            attributes_map=self._attributes_map(attributes) if include_attributes else None,
            raw=ad if include_raw else None,
        )

    @staticmethod
    def _price(ad: dict[str, Any]) -> float | None:
        cents = ad.get("price_cents")
        if isinstance(cents, (int, float)):
            return float(cents) / 100
        price = ad.get("price")
        if isinstance(price, list) and price:
            price = price[0]
        try:
            return float(price) if price is not None else None
        except (TypeError, ValueError):
            return None

    def _brand(self, value: Any, attributes: dict[str, dict[str, Any]]) -> str | None:
        for key, attribute in attributes.items():
            if key == "brand" or key.endswith("_brand"):
                brand = self._clean_text(attribute.get("value_label") or attribute.get("value"))
                if brand:
                    return brand
        if not isinstance(value, str):
            return None
        clean = value.strip()
        if not clean or clean.lower() == "leboncoin":
            return None
        return clean

    @staticmethod
    def _images(ad: dict[str, Any]) -> list[str]:
        raw = ad.get("images") or {}
        if not isinstance(raw, dict):
            return []
        for key in ["urls_large", "urls", "thumb_url"]:
            value = raw.get(key)
            if isinstance(value, list):
                return [url for url in value if isinstance(url, str)]
            if isinstance(value, str):
                return [value]
        return []

    @staticmethod
    def _image_count(ad: dict[str, Any], images: list[str]) -> int | None:
        raw = ad.get("images") or {}
        if isinstance(raw, dict) and isinstance(raw.get("nb_images"), int):
            return raw["nb_images"]
        return len(images) or None

    @staticmethod
    def _compact_location(location: Any, *, include_coordinates: bool) -> dict[str, Any] | None:
        if not isinstance(location, dict):
            return None
        keys = ["region_name", "department_name", "city_label", "city", "zipcode"]
        if include_coordinates:
            keys += ["lat", "lng"]
        return {key: location.get(key) for key in keys if location.get(key) is not None}

    @staticmethod
    def _compact_owner(owner: Any) -> dict[str, Any] | None:
        if not isinstance(owner, dict):
            return None
        keys = ["user_id", "name", "type", "profile_id", "store_id", "siren"]
        return {key: owner.get(key) for key in keys if owner.get(key) is not None}

    def _seller_rating(self, attributes: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        score = self._float_value(self._attribute_value(attributes, "rating_score"))
        count = self._int_value(self._attribute_value(attributes, "rating_count"))
        if score is None and count is None:
            return None
        return {key: value for key, value in {"score": score, "count": count}.items() if value is not None}

    def _shipping(self, attributes: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        data = {
            "shippable": self._bool_value(self._attribute_value(attributes, "shippable")),
            "methods": self._attribute_values(attributes, "shipping_type"),
            "parcel_size": self._attribute_value(attributes, "estimated_parcel_size"),
            "parcel_weight_g": self._int_value(self._attribute_value(attributes, "estimated_parcel_weight")),
            "bundleable": self._bool_value(self._attribute_value(attributes, "is_bundleable")),
            "purchase_available": self._bool_value(self._attribute_value(attributes, "purchase_cta_visible")),
            "negotiation_available": self._bool_value(self._attribute_value(attributes, "negotiation_cta_visible")),
        }
        compact = {key: value for key, value in data.items() if value not in (None, [], {})}
        return compact or None

    @staticmethod
    def _compact_options(options: Any, is_boosted: Any) -> dict[str, Any] | None:
        compact: dict[str, Any] = {}
        if isinstance(options, dict):
            for key in ["booster", "urgent", "gallery", "photosup", "sub_toplist", "highlight", "display_as_alu"]:
                value = options.get(key)
                if value is not None:
                    compact[key] = value
        if is_boosted is not None:
            compact["is_boosted"] = is_boosted
        return compact or None

    @staticmethod
    def _compact_attributes(attributes: Any) -> list[dict[str, Any]] | None:
        if not isinstance(attributes, list):
            return None
        compact = []
        for attribute in attributes:
            if not isinstance(attribute, dict):
                continue
            compact.append(
                {
                    key: value
                    for key, value in {
                        "key": attribute.get("key"),
                        "label": attribute.get("key_label"),
                        "value": attribute.get("value_label") or attribute.get("value"),
                        "values": attribute.get("values_label") or attribute.get("values"),
                    }.items()
                    if value is not None
                }
            )
        return compact or None

    @staticmethod
    def _attribute_lookup(attributes: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(attributes, list):
            return {}
        lookup: dict[str, dict[str, Any]] = {}
        for attribute in attributes:
            if isinstance(attribute, dict) and isinstance(attribute.get("key"), str):
                lookup[attribute["key"]] = attribute
        return lookup

    @staticmethod
    def _attributes_map(attributes: Any) -> dict[str, Any] | None:
        compact = LeboncoinClient._compact_attributes(attributes)
        if not compact:
            return None
        mapped = {}
        for attribute in compact:
            key = attribute.get("key")
            if not key:
                continue
            mapped[key] = {name: value for name, value in attribute.items() if name != "key"}
        return mapped or None

    @staticmethod
    def _attribute_value(attributes: dict[str, dict[str, Any]], key: str) -> str | None:
        attribute = attributes.get(key)
        if not attribute:
            return None
        value = attribute.get("value_label") or attribute.get("value")
        return LeboncoinClient._clean_text(value)

    @staticmethod
    def _attribute_values(attributes: dict[str, dict[str, Any]], key: str) -> list[str] | None:
        attribute = attributes.get(key)
        if not attribute:
            return None
        values = attribute.get("values_label") or attribute.get("values")
        if isinstance(values, list):
            clean_values = [LeboncoinClient._clean_text(value) for value in values]
            return [value for value in clean_values if value]
        clean = LeboncoinClient._clean_text(values)
        return [clean] if clean else None

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        if value is None:
            return None
        clean = str(value).strip()
        return clean or None

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

    @staticmethod
    def _bool_value(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if value is None:
            return None
        clean = str(value).strip().lower()
        if clean in {"true", "1", "yes", "oui"}:
            return True
        if clean in {"false", "0", "no", "non"}:
            return False
        return None

    def _category_info(self, category_id: Any) -> tuple[str | None, list[str] | None]:
        if category_id is None or not lbc:
            return None, None
        category_value = str(category_id)
        for category in lbc.Category:
            if str(category.value) == category_value:
                key = category.name.lower()
                parent_names = [
                    item.name
                    for item in lbc.Category
                    if item.name != category.name and category.name.startswith(f"{item.name}_")
                ]
                if parent_names:
                    parent = max(parent_names, key=len)
                    child = category.name.removeprefix(f"{parent}_")
                    return key, [self._humanize_category_part(parent), self._humanize_category_part(child)]
                return key, [self._humanize_category_part(category.name)]
        return None, None

    @staticmethod
    def _humanize_category_part(value: str) -> str:
        return value.replace("DEMPLOI", "D'EMPLOI").replace("_", " ").title()

    @staticmethod
    def _absolute_url(url: Any) -> str | None:
        if not isinstance(url, str) or not url:
            return None
        if url.startswith("http"):
            return url
        if url.startswith("/"):
            return f"{LEBONCOIN_HOST}{url}"
        return f"{LEBONCOIN_HOST}/{url}"

    @staticmethod
    def _key(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value)
        ascii_value = "".join(char for char in normalized if not unicodedata.combining(char))
        return re.sub(r"[^a-z0-9]+", "_", ascii_value.lower()).strip("_")
