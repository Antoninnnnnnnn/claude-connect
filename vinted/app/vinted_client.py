import asyncio
import hashlib
import html as html_lib
import json
import logging
import re
import statistics
import time
from typing import Any

import httpx

from app.config import Settings
from app.models import PriceStats, SearchResult


logger = logging.getLogger(__name__)


DOMAIN_HOSTS = {
    "fr": "www.vinted.fr",
    "de": "www.vinted.de",
    "it": "www.vinted.it",
    "es": "www.vinted.es",
}

ORDER_MAP = {
    "newest": "newest_first",
    "newest_first": "newest_first",
    "relevance": "relevance",
    "price_low": "price_low_to_high",
    "price_low_to_high": "price_low_to_high",
    "price_high": "price_high_to_low",
    "price_high_to_low": "price_high_to_low",
}

CONDITION_IDS = {
    "new_with_tags": "6",
    "new": "6",
    "new_without_tags": "1",
    "very_good": "2",
    "good": "3",
    "satisfactory": "4",
}


class VintedError(RuntimeError):
    pass


class VintedClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._cookie_fingerprint: str | None = None
        self._session_refreshes = 0
        self._last_error: str | None = None

    def health_status(self) -> dict[str, Any]:
        return {
            "status": "up",
            "started": self._client is not None,
            "proxy_configured": bool(self.settings.vinted_proxy),
            "has_session_cookies": bool(self._cookie_fingerprint),
            "session_refreshes": self._session_refreshes,
            "last_error": self._last_error,
        }

    async def start(self) -> None:
        self.settings.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.vinted_timeout),
            proxy=self.settings.vinted_proxy,
            follow_redirects=True,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
                ),
            },
        )
        self._load_cookies()

    async def close(self) -> None:
        if self._client:
            self._save_cookies()
            await self._client.aclose()

    def _base_url(self, domain: str | None) -> str:
        key = (domain or self.settings.default_domain).lower().strip()
        key = key.removeprefix("https://").removeprefix("http://")
        key = key.removeprefix("www.vinted.")
        host = DOMAIN_HOSTS.get(key)
        if not host:
            if key in DOMAIN_HOSTS.values():
                host = key
            else:
                raise VintedError(f"Unsupported Vinted domain: {domain}")
        return f"https://{host}"

    def _load_cookies(self) -> None:
        if not self._client or not self.settings.cookie_file.exists():
            return
        try:
            payload = json.loads(self.settings.cookie_file.read_text(encoding="utf-8"))
            for cookie in payload:
                self._client.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain"),
                    path=cookie.get("path", "/"),
                )
        except Exception:
            return

    def _save_cookies(self) -> None:
        """Persist the cookie jar, but only when it actually changed.

        This used to run on every successful request, rewriting the same bytes to
        disk dozens of times per search.
        """
        if not self._client:
            return
        cookies = [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
            }
            for cookie in self._client.cookies.jar
        ]
        payload = json.dumps(cookies, indent=2)
        fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if fingerprint == self._cookie_fingerprint:
            return
        try:
            self.settings.cookie_file.parent.mkdir(parents=True, exist_ok=True)
            self.settings.cookie_file.write_text(payload, encoding="utf-8")
            self.settings.cookie_file.chmod(0o600)
        except OSError as exc:
            logger.warning("Could not persist Vinted cookies: %s", exc)
            return
        self._cookie_fingerprint = fingerprint

    async def refresh_session(self, domain: str | None = None) -> None:
        if not self._client:
            raise VintedError("HTTP client is not started")
        base_url = self._base_url(domain)
        response = await self._client.get(
            base_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": base_url,
            },
        )
        if response.status_code >= 400:
            self._last_error = f"session refresh HTTP {response.status_code}"
            raise VintedError(f"Vinted session refresh failed: HTTP {response.status_code}")
        self._session_refreshes += 1
        self._last_error = None
        self._save_cookies()

    async def _throttle(self) -> None:
        async with self._lock:
            elapsed = time.monotonic() - self._last_request
            wait_for = self.settings.vinted_min_interval - elapsed
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_request = time.monotonic()

    async def _request(self, domain: str | None, path: str, params: dict[str, Any] | list[tuple[str, Any]] | None = None) -> Any:
        if not self._client:
            raise VintedError("HTTP client is not started")
        base_url = self._base_url(domain)
        url = f"{base_url}{path}"
        last_error = None

        for attempt in range(self.settings.vinted_max_retries + 1):
            await self._throttle()
            try:
                response = await self._client.get(url, params=params, headers={"Referer": base_url})
                if response.status_code in {401, 403} and attempt == 0:
                    await self.refresh_session(domain)
                    continue
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    last_error = f"Vinted HTTP {response.status_code}"
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                if response.status_code >= 400:
                    raise VintedError(f"Vinted HTTP {response.status_code}: {response.text[:250]}")
                self._save_cookies()
                return response.json()
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                if attempt == 0:
                    await self.refresh_session(domain)
                await asyncio.sleep(min(2**attempt, 8))

        raise VintedError(f"Vinted request failed after retries: {last_error}")

    async def _request_text(self, domain: str | None, path: str) -> tuple[str, str]:
        if not self._client:
            raise VintedError("HTTP client is not started")
        base_url = self._base_url(domain)
        url = f"{base_url}{path}"
        last_error = None

        for attempt in range(self.settings.vinted_max_retries + 1):
            await self._throttle()
            try:
                response = await self._client.get(
                    url,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": base_url,
                    },
                )
                if response.status_code in {401, 403} and attempt == 0:
                    await self.refresh_session(domain)
                    continue
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    last_error = f"Vinted HTTP {response.status_code}"
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                if response.status_code >= 400:
                    raise VintedError(f"Vinted HTTP {response.status_code}: {response.text[:250]}")
                self._save_cookies()
                return response.text, str(response.url)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                if attempt == 0:
                    await self.refresh_session(domain)
                await asyncio.sleep(min(2**attempt, 8))

        raise VintedError(f"Vinted HTML request failed after retries: {last_error}")

    async def search(
        self,
        *,
        query: str | None = None,
        brand: str | None = None,
        size: str | None = None,
        condition: str | None = None,
        price_min: float | None = None,
        price_max: float | None = None,
        catalog: str | None = None,
        order: str = "newest",
        domain: str | None = None,
        per_page: int = 24,
        page: int = 1,
        include_photo: bool = False,
        include_seller: bool = False,
        raw: bool = False,
    ) -> dict[str, Any]:
        params: list[tuple[str, Any]] = [
            ("page", page),
            ("per_page", min(max(per_page, 1), 96)),
            ("order", ORDER_MAP.get(order, order)),
            ("currency", "EUR"),
        ]

        search_text = " ".join(part for part in [query, None if self._is_ids(brand) else brand] if part)
        if search_text:
            params.append(("search_text", search_text))
        if price_min is not None:
            params.append(("price_from", price_min))
        if price_max is not None:
            params.append(("price_to", price_max))
        self._append_ids(params, "catalog_ids[]", catalog)
        self._append_ids(params, "brand_ids[]", brand)
        self._append_ids(params, "size_ids[]", size)
        status_id = self._condition_to_id(condition)
        if status_id:
            params.append(("status_ids[]", status_id))

        payload = await self._request(domain, "/api/v2/catalog/items", params=params)
        items = payload.get("items") or []
        return {
            "items": [
                self._normalize_item(
                    item,
                    include_photo=include_photo,
                    include_seller=include_seller,
                    include_raw=raw,
                ).model_dump(exclude_none=True)
                for item in items
            ],
            "pagination": {
                "page": page,
                "per_page": min(max(per_page, 1), 96),
                "count": len(items),
            },
            "raw": payload if raw else None,
        }

    async def item(
        self,
        item_id: int,
        *,
        domain: str | None = None,
        include_photo: bool = False,
        include_seller: bool = False,
        raw: bool = False,
    ) -> dict[str, Any]:
        try:
            payload = await self._request(domain, f"/api/v2/items/{item_id}/details")
        except VintedError as exc:
            item = await self._item_from_public_page(
                item_id,
                domain=domain,
                include_photo=include_photo,
                include_seller=include_seller,
                raw=raw,
            )
            item["api_details_error"] = str(exc) if raw else None
            return {key: value for key, value in item.items() if value is not None}
        item = payload.get("item") or payload
        data = {
            "item": self._normalize_item(
                item,
                include_photo=include_photo,
                include_seller=include_seller,
                include_raw=raw,
            ).model_dump(exclude_none=True),
            "source": "details_api",
            "details_available": True,
        }
        if raw:
            data["raw"] = payload
        return data

    async def _item_from_public_page(
        self,
        item_id: int,
        *,
        domain: str | None,
        include_photo: bool,
        include_seller: bool,
        raw: bool,
    ) -> dict[str, Any]:
        html, final_url = await self._request_text(domain, f"/items/{item_id}")
        parsed = self._parse_item_page(html, final_url)
        if not parsed.get("id"):
            parsed["id"] = item_id
        return {
            "item": self._normalize_item(
                parsed,
                include_photo=include_photo,
                include_seller=include_seller,
                include_raw=raw,
            ).model_dump(exclude_none=True),
            "source": "item_page",
            "details_available": True,
            "raw": parsed if raw else None,
        }

    def _parse_item_page(self, html: str, final_url: str) -> dict[str, Any]:
        structured = self._extract_product_json(html)
        offers = structured.get("offers") if isinstance(structured.get("offers"), dict) else {}
        brand = structured.get("brand") if isinstance(structured.get("brand"), dict) else {}

        price = offers.get("price")
        currency = offers.get("priceCurrency")
        total_price = self._parse_price_text(self._extract_testid_text(html, "total-combined-price"))
        item_price = price if price is not None else self._parse_price_text(self._extract_testid_text(html, "item-price"))
        title = structured.get("name") or self._extract_meta(html, "og:title")
        if title and title.endswith(" | Vinted"):
            title = title.removesuffix(" | Vinted")

        photos = self._extract_photo_urls(html)
        seller = self._extract_seller(html)
        categories, category_ids = self._extract_breadcrumbs(html)
        color = self._extract_item_attribute(html, "item-attributes-color", "color") or structured.get("color")
        upload_date = self._extract_item_attribute(html, "item-attributes-upload_date", "upload_date") or self._extract_added_at_text(html)
        shipping_text = self._extract_testid_text(html, "item-shipping-banner-price")

        return {
            "id": self._extract_item_id(final_url),
            "title": title,
            "description": structured.get("description") or self._extract_meta(html, "description"),
            "brand": brand.get("name"),
            "brand_id": self._extract_brand_id(html),
            "size": self._extract_item_attribute(html, "item-attributes-size", "size"),
            "status": self._extract_item_attribute(html, "item-attributes-status", "status"),
            "price": {"amount": str(item_price), "currency_code": currency} if item_price is not None else None,
            "total_item_price": {"amount": str(total_price), "currency_code": currency} if total_price is not None else None,
            "shipping_price": self._parse_price_text(shipping_text),
            "shipping_text": shipping_text,
            "url": offers.get("url") or final_url,
            "photo": {"url": structured.get("image") or (photos[0] if photos else None)},
            "photos": photos,
            "seller": seller,
            "category": " > ".join(categories) or structured.get("category") or None,
            "category_leaf": categories[-1] if categories else structured.get("category"),
            "categories": categories,
            "category_ids": category_ids,
            "color": color,
            "status_schema": offers.get("itemCondition"),
            "availability": offers.get("availability"),
            "upload_date": upload_date,
            "service_fee_included": self._extract_testid_text(html, "service-fee-included-title") is not None,
        }

    @staticmethod
    def _extract_product_json(html: str) -> dict[str, Any]:
        for match in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
            try:
                data = json.loads(html_lib.unescape(match.group(1)))
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("@type") == "Product":
                return data
        return {}

    @staticmethod
    def _extract_meta(html: str, name: str) -> str | None:
        patterns = [
            rf'<meta name="{re.escape(name)}" content="([^"]*)"',
            rf'<meta property="{re.escape(name)}" content="([^"]*)"',
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                return html_lib.unescape(match.group(1)).strip()
        return None

    @staticmethod
    def _extract_item_id(url: str) -> int | None:
        match = re.search(r"/items/(\d+)", url)
        return int(match.group(1)) if match else None

    @staticmethod
    def _extract_testid_text(html: str, testid: str) -> str | None:
        match = re.search(rf'data-testid="{re.escape(testid)}"[^>]*>([^<]+)<', html)
        return html_lib.unescape(match.group(1)).strip() if match else None

    @staticmethod
    def _extract_item_attribute(html: str, testid: str, item_prop: str) -> str | None:
        pattern = rf'data-testid="{re.escape(testid)}".*?itemProp="{re.escape(item_prop)}".*?web_ui__Text__bold">([^<]+)'
        match = re.search(pattern, html, re.S)
        return html_lib.unescape(match.group(1)).strip() if match else None

    @staticmethod
    def _parse_price_text(value: str | None) -> float | None:
        if not value:
            return None
        clean = re.sub(r"[^0-9,.]", "", value).replace(",", ".")
        try:
            return float(clean)
        except ValueError:
            return None

    @staticmethod
    def _extract_photo_urls(html: str) -> list[str]:
        urls = []
        for tag in re.findall(r"<img[^>]+>", html):
            if "item-photo-" not in tag:
                continue
            match = re.search(r'src="([^"]+)"', tag)
            if match:
                url = html_lib.unescape(match.group(1))
                if url not in urls:
                    urls.append(url)
        return urls

    @staticmethod
    def _extract_breadcrumbs(html: str) -> tuple[list[str], list[int]]:
        values: list[str] = []
        ids: list[int] = []
        for href, value in re.findall(r'<a href="/catalog/([^"]+)" itemProp="url"><span itemProp="title">(.*?)</span>', html):
            clean = html_lib.unescape(re.sub(r"<.*?>", "", value)).strip()
            if clean and clean not in values:
                values.append(clean)
            match = re.match(r"(\d+)", href)
            if match:
                catalog_id = int(match.group(1))
                if catalog_id not in ids:
                    ids.append(catalog_id)
        return values, ids

    @staticmethod
    def _extract_brand_id(html: str) -> int | None:
        match = re.search(r'href="/brand/(\d+)-', html)
        if not match:
            match = re.search(r"/brand/(\d+)-", html)
        return int(match.group(1)) if match else None

    @staticmethod
    def _extract_seller(html: str) -> dict[str, Any] | None:
        seller: dict[str, Any] = {}
        receiver_id = re.search(r"receiver_id=(\d+)", html)
        member_id = re.search(r'href="/member/(\d+)"', html)
        if receiver_id or member_id:
            seller["id"] = int((receiver_id or member_id).group(1))
        username = re.search(r'data-testid="profile-username"[^>]*>([^<]+)<', html)
        if username:
            seller["login"] = html_lib.unescape(username.group(1)).strip()
        if seller.get("id"):
            seller["profile_url"] = f"https://www.vinted.fr/member/{seller['id']}"
        location = re.search(r'data-testid="seller-location"[^>]*>([^<]+)<', html)
        if location:
            seller["location"] = html_lib.unescape(location.group(1)).strip()
        last_seen = re.search(r'data-testid="seller-last-logged-in"[^>]*>([^<]+)<', html)
        if last_seen:
            seller["last_seen"] = html_lib.unescape(last_seen.group(1)).strip()
        # `\s` must reach the regex engine as whitespace: the previous `\\s` matched a
        # literal backslash, so no rating was ever extracted.
        rating = re.search(r'aria-label="[^"]*noté\s+([0-9,.]+)\s+sur\s+5"', html)
        if rating:
            seller["rating"] = float(rating.group(1).replace(",", "."))
        return seller or None

    @staticmethod
    def _extract_added_at_text(html: str) -> str | None:
        match = re.search(r">(Ajouté[^<]+)<", html)
        return html_lib.unescape(match.group(1)).strip() if match else None

    async def price_stats(self, **kwargs: Any) -> PriceStats:
        kwargs["per_page"] = min(int(kwargs.pop("per_page", 96) or 96), 96)
        result = await self.search(**kwargs)
        prices = [item["price"] for item in result["items"] if item.get("price") is not None]
        currencies = [item["currency"] for item in result["items"] if item.get("currency")]
        return PriceStats(
            count=len(prices),
            min=min(prices) if prices else None,
            max=max(prices) if prices else None,
            median=statistics.median(prices) if prices else None,
            currency=currencies[0] if currencies else None,
            sample_size=len(result["items"]),
        )

    @staticmethod
    def _is_ids(value: str | None) -> bool:
        if not value:
            return False
        return all(part.strip().isdigit() for part in value.split(",") if part.strip())

    @staticmethod
    def _append_ids(params: list[tuple[str, Any]], name: str, value: str | None) -> None:
        if not value:
            return
        for part in value.split(","):
            clean = part.strip()
            if clean.isdigit():
                params.append((name, clean))

    @staticmethod
    def _condition_to_id(condition: str | None) -> str | None:
        if not condition:
            return None
        clean = condition.lower().strip().replace(" ", "_").replace("-", "_")
        if clean.isdigit():
            return clean
        return CONDITION_IDS.get(clean)

    @staticmethod
    def _money(value: Any) -> tuple[float | None, str | None]:
        if isinstance(value, dict):
            amount = value.get("amount") or value.get("value")
            currency = value.get("currency_code") or value.get("currency")
        else:
            amount = value
            currency = None
        try:
            return (float(str(amount).replace(",", ".")), currency) if amount is not None else (None, currency)
        except ValueError:
            return None, currency

    def _normalize_item(
        self,
        item: dict[str, Any],
        *,
        include_photo: bool = False,
        include_seller: bool = False,
        include_raw: bool = False,
    ) -> SearchResult:
        price, currency = self._money(item.get("price"))
        total_price, total_currency = self._money(item.get("total_item_price"))
        photo = item.get("photo") or {}
        seller = item.get("user") or item.get("seller")
        return SearchResult(
            id=item.get("id"),
            title=item.get("title"),
            brand=item.get("brand_title") or item.get("brand"),
            size=item.get("size_title") or item.get("size"),
            condition=item.get("status") or item.get("status_name"),
            description=item.get("description"),
            category=item.get("category"),
            category_leaf=item.get("category_leaf"),
            categories=item.get("categories"),
            category_ids=item.get("category_ids"),
            color=item.get("color"),
            brand_id=item.get("brand_id"),
            price=price,
            currency=currency or total_currency or item.get("currency"),
            total_item_price=total_price,
            shipping_price=item.get("shipping_price"),
            shipping_text=item.get("shipping_text"),
            upload_date=item.get("upload_date"),
            availability=item.get("availability"),
            status_schema=item.get("status_schema") or item.get("item_condition_schema"),
            service_fee_included=item.get("service_fee_included"),
            url=item.get("url"),
            photo=photo.get("url") if include_photo and isinstance(photo, dict) else (photo if include_photo else None),
            photos=item.get("photos") if include_photo and isinstance(item.get("photos"), list) else None,
            seller=self._compact_seller(seller) if include_seller and isinstance(seller, dict) else None,
            raw=item if include_raw else None,
        )

    @staticmethod
    def _compact_seller(seller: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "id": seller.get("id"),
                "login": seller.get("login"),
                "profile_url": seller.get("profile_url"),
                "business": seller.get("business"),
                "location": seller.get("location"),
                "last_seen": seller.get("last_seen"),
                "rating": seller.get("rating"),
            }.items()
            if value is not None
        }
