from contextlib import asynccontextmanager
import asyncio
import logging
import secrets
from typing import Any

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.centrale_client import CentraleClient, CentraleError, CentraleNotFoundError
from app.config import Settings, get_settings
from app.params import SearchFilters, effective_distance, search_filters_dependency, validate_ranges


logger = logging.getLogger(__name__)
settings = get_settings()
centrale = CentraleClient(settings)
MAX_LIMIT = settings.max_fetchable_limit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.api_key:
        raise RuntimeError("API_KEY is not configured")
    await anyio.to_thread.run_sync(centrale.bootstrap)
    if settings.centrale_warmup_on_start:
        asyncio.create_task(asyncio.to_thread(centrale.warmup))
    try:
        yield
    finally:
        await anyio.to_thread.run_sync(centrale.close)


app = FastAPI(
    title="Self-hosted La Centrale Search API",
    version="1.1.1",
    docs_url="/docs",
    lifespan=lifespan,
)


@app.exception_handler(CentraleNotFoundError)
async def centrale_not_found_handler(_: Request, exc: CentraleNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"ok": False, "error": str(exc)})


@app.exception_handler(CentraleError)
async def centrale_error_handler(_: Request, exc: CentraleError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"ok": False, "error": str(exc)})


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if not isinstance(detail, str):
        detail = str(detail)
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": detail})


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
    return {"ok": True, "data": centrale.health_status()}


@app.post("/warmup", dependencies=[Depends(require_api_key)])
async def warmup() -> dict[str, Any]:
    data = await anyio.to_thread.run_sync(centrale.warmup)
    return {"ok": True, "data": data}


@app.get("/metadata", dependencies=[Depends(require_api_key)])
async def metadata(
    make: str | None = Query(default=None),
    model: str | None = Query(default=None),
    version: str | None = Query(default=None),
    price_max: float | None = Query(default=None, ge=0),
    zip: str | None = Query(default=None, pattern=r"^\d{5}$"),
    distance_km: int | None = Query(default=None, ge=0),
    distance_bucket: int | None = Query(default=None, ge=0),
    energy: str | None = Query(default=None),
) -> dict[str, Any]:
    data = await anyio.to_thread.run_sync(
        lambda: centrale.metadata(
            make=make,
            model=model,
            version=version,
            price_max=price_max,
            zip=zip,
            distance_km=effective_distance(distance_km, distance_bucket),
            energy=energy,
        )
    )
    return {"ok": True, "data": data}


@app.get("/search", dependencies=[Depends(require_api_key)])
async def search(
    filters: SearchFilters = Depends(search_filters_dependency),
    limit: int = Query(default=20, ge=1, le=MAX_LIMIT),
    include_image: bool = Query(default=False),
    include_dealer: bool = Query(default=False),
    include_vehicle: bool = Query(default=False),
    debug: bool = Query(default=False),
    raw: bool = Query(default=False),
) -> dict[str, Any]:
    validate_ranges(
        filters.price_min,
        filters.price_max,
        filters.year_min,
        filters.year_max,
        filters.mileage_min,
        filters.mileage_max,
    )
    kwargs = filters.client_kwargs()
    data = await anyio.to_thread.run_sync(
        lambda: centrale.search(
            **kwargs,
            limit=limit,
            include_image=include_image,
            include_dealer=include_dealer,
            include_vehicle=include_vehicle,
            debug=debug,
            raw=raw,
        )
    )
    return {"ok": True, "data": data}


@app.get("/listing/{ref}", dependencies=[Depends(require_api_key)])
async def get_listing(
    ref: str = Path(..., min_length=3, pattern=r"^[A-Za-z]\d+$"),
    include_image: bool = Query(default=True),
    include_dealer: bool = Query(default=False),
    include_vehicle: bool = Query(default=True),
    include_description: bool = Query(default=False),
    raw: bool = Query(default=False),
    debug: bool = Query(default=False),
) -> dict[str, Any]:
    data = await anyio.to_thread.run_sync(
        lambda: centrale.listing(
            ref,
            include_image=include_image,
            include_dealer=include_dealer,
            include_vehicle=include_vehicle,
            include_description=include_description,
            raw=raw,
            debug=debug,
        )
    )
    return {"ok": True, "data": data}


@app.get("/listings", dependencies=[Depends(require_api_key)])
async def get_listings(
    refs: str = Query(..., description="Comma-separated listing references, e.g. B104008382,W102941021"),
    include_image: bool = Query(default=True),
    include_dealer: bool = Query(default=False),
    include_vehicle: bool = Query(default=True),
    include_description: bool = Query(default=False),
    raw: bool = Query(default=False),
    debug: bool = Query(default=False),
    max_workers: int = Query(default=4, ge=1, le=8),
) -> dict[str, Any]:
    ref_list = [part.strip() for part in refs.split(",") if part.strip()]
    data = await anyio.to_thread.run_sync(
        lambda: centrale.listings(
            ref_list,
            max_workers=max_workers,
            include_image=include_image,
            include_dealer=include_dealer,
            include_vehicle=include_vehicle,
            include_description=include_description,
            raw=raw,
            debug=debug,
        )
    )
    return {"ok": True, "data": data}


@app.get("/price-stats", dependencies=[Depends(require_api_key)])
async def price_stats(
    filters: SearchFilters = Depends(search_filters_dependency),
    limit: int = Query(default=70, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    validate_ranges(
        filters.price_min,
        filters.price_max,
        filters.year_min,
        filters.year_max,
        filters.mileage_min,
        filters.mileage_max,
    )
    kwargs = filters.client_kwargs()
    kwargs["limit"] = limit
    stats = await anyio.to_thread.run_sync(lambda: centrale.price_stats(**kwargs))
    return {"ok": True, "data": stats.model_dump()}
