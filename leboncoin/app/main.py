import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.lbc_client import LeboncoinClient, LeboncoinError


logger = logging.getLogger(__name__)
settings = get_settings()
leboncoin = LeboncoinClient(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.api_key:
        raise RuntimeError("API_KEY is not configured")
    try:
        yield
    finally:
        await anyio.to_thread.run_sync(leboncoin.close)


app = FastAPI(
    title="Self-hosted Leboncoin Search API",
    version="1.1.0",
    docs_url="/docs",
    lifespan=lifespan,
)


@app.exception_handler(LeboncoinError)
async def leboncoin_error_handler(_: Request, exc: LeboncoinError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"ok": False, "error": str(exc)})


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": str(exc.detail)})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"ok": False, "error": str(exc.errors())})


@app.exception_handler(Exception)
async def generic_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"ok": False, "error": "Internal server error"})


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    current_settings: Settings = Depends(get_settings),
) -> None:
    if not current_settings.api_key:
        raise HTTPException(status_code=500, detail="API_KEY is not configured")
    if not x_api_key or not secrets.compare_digest(x_api_key, current_settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/health")
async def health() -> dict[str, Any]:
    proxy_count = len(settings.proxy_urls())
    return {"ok": True, "data": {"status": "up", "proxy_configured": proxy_count > 0, "proxy_count": proxy_count}}


@app.get("/metadata", dependencies=[Depends(require_api_key)])
async def metadata() -> dict[str, Any]:
    data = await anyio.to_thread.run_sync(leboncoin.metadata)
    return {"ok": True, "data": data}


@app.get("/search", dependencies=[Depends(require_api_key)])
async def search(
    text: str | None = Query(default=None),
    category: str | None = Query(default=None, description="Numeric Leboncoin category ID or name, e.g. sneakers, chaussures, mode."),
    location: str | None = Query(default=None, description="dept:75, region:12, paris, ile_de_france, d_75, r_12, or lat,lng,radius."),
    lat: float | None = Query(default=None),
    lng: float | None = Query(default=None),
    radius: int | None = Query(default=None, ge=1000, le=200000),
    price_min: float | None = Query(default=None, ge=0),
    price_max: float | None = Query(default=None, ge=0),
    sort: str = Query(default="newest", description="newest, oldest, relevance, price_low, price_high."),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=105),
    url: str | None = Query(default=None, description="Full Leboncoin search URL. Overrides text/category/location filters."),
    include_image: bool = Query(default=False, description="Add only the main image URL."),
    include_images: bool = Query(default=False, description="Add all image URLs. Verbose."),
    include_body: bool = Query(default=False, description="Add ad description/body. Verbose on search."),
    include_owner: bool = Query(default=False, description="Add compact seller info."),
    include_attributes: bool = Query(default=False, description="Add Leboncoin attributes. Verbose."),
    include_coordinates: bool = Query(default=False, description="Add lat/lng in location."),
    debug: bool = Query(default=False, description="Add source/failure diagnostics."),
    raw: bool = Query(default=False),
) -> dict[str, Any]:
    data = await anyio.to_thread.run_sync(
        lambda: leboncoin.search(
            text=text,
            category=category,
            location=location,
            lat=lat,
            lng=lng,
            radius=radius,
            price_min=price_min,
            price_max=price_max,
            sort=sort,
            page=page,
            limit=limit,
            url=url,
            include_image=include_image,
            include_images=include_images,
            include_body=include_body,
            include_owner=include_owner,
            include_attributes=include_attributes,
            include_coordinates=include_coordinates,
            debug=debug,
            raw=raw,
        )
    )
    return {"ok": True, "data": data}


@app.get("/ad/{ad_id}", dependencies=[Depends(require_api_key)])
async def get_ad(
    ad_id: int = Path(..., ge=1),
    include_image: bool = Query(default=False, description="Add only the main image URL."),
    include_images: bool = Query(default=False, description="Add all image URLs. Verbose."),
    include_body: bool = Query(default=True, description="Add ad description/body. Enabled by default for detail endpoint."),
    include_owner: bool = Query(default=False),
    include_attributes: bool = Query(default=False),
    include_coordinates: bool = Query(default=False),
    debug: bool = Query(default=False, description="Add source diagnostics."),
    raw: bool = Query(default=False),
) -> dict[str, Any]:
    data = await anyio.to_thread.run_sync(
        lambda: leboncoin.ad(
            ad_id,
            include_image=include_image,
            include_images=include_images,
            include_body=include_body,
            include_owner=include_owner,
            include_attributes=include_attributes,
            include_coordinates=include_coordinates,
            debug=debug,
            raw=raw,
        )
    )
    return {"ok": True, "data": data}


@app.get("/price-stats", dependencies=[Depends(require_api_key)])
async def price_stats(
    text: str | None = Query(default=None),
    category: str | None = Query(default=None),
    location: str | None = Query(default=None),
    lat: float | None = Query(default=None),
    lng: float | None = Query(default=None),
    radius: int | None = Query(default=None, ge=1000, le=200000),
    price_min: float | None = Query(default=None, ge=0),
    price_max: float | None = Query(default=None, ge=0),
    sort: str = Query(default="newest"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=70, ge=1, le=105),
    url: str | None = Query(default=None),
) -> dict[str, Any]:
    stats = await anyio.to_thread.run_sync(
        lambda: leboncoin.price_stats(
            text=text,
            category=category,
            location=location,
            lat=lat,
            lng=lng,
            radius=radius,
            price_min=price_min,
            price_max=price_max,
            sort=sort,
            page=page,
            limit=limit,
            url=url,
            include_image=False,
            include_images=False,
            include_body=False,
            include_owner=False,
            include_attributes=False,
            include_coordinates=False,
            raw=False,
        )
    )
    return {"ok": True, "data": stats.model_dump()}
