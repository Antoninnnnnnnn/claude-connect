import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.vinted_client import VintedClient, VintedError


logger = logging.getLogger(__name__)
settings = get_settings()
vinted = VintedClient(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.api_key:
        raise RuntimeError("API_KEY is not configured")
    await vinted.start()
    try:
        # Best effort: a Vinted outage at boot must not turn into a systemd restart
        # loop. The first real request refreshes the session anyway.
        await vinted.refresh_session(settings.default_domain)
    except Exception as exc:  # noqa: BLE001 - upstream may be down or rate limiting
        logger.warning("Initial Vinted session refresh failed, continuing: %s", exc)
    try:
        yield
    finally:
        await vinted.close()


app = FastAPI(
    title="Self-hosted Vinted Search API",
    version="1.1.0",
    docs_url="/docs",
    lifespan=lifespan,
)


@app.exception_handler(VintedError)
async def vinted_error_handler(_: Request, exc: VintedError) -> JSONResponse:
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
    return {"ok": True, "data": vinted.health_status()}


@app.get("/search", dependencies=[Depends(require_api_key)])
async def search(
    query: str | None = Query(default=None),
    brand: str | None = Query(default=None, description="Brand name fallback, or Vinted brand ID(s) comma-separated."),
    size: str | None = Query(default=None, description="Vinted size ID(s) comma-separated."),
    condition: str | None = Query(default=None, description="new, new_without_tags, very_good, good, satisfactory, or Vinted status ID."),
    price_min: float | None = Query(default=None, ge=0),
    price_max: float | None = Query(default=None, ge=0),
    catalog: str | None = Query(default=None, description="Vinted catalog ID(s) comma-separated."),
    order: str = Query(default="newest"),
    domain: str | None = Query(default=None, description="fr, de, it, es, or full www.vinted.* host."),
    per_page: int = Query(default=24, ge=1, le=96),
    page: int = Query(default=1, ge=1),
    include_photo: bool = Query(default=False, description="Add the main image URL. Disabled by default to keep responses compact."),
    include_seller: bool = Query(default=False, description="Add compact seller info. Disabled by default to keep responses compact."),
    raw: bool = Query(default=False, description="Add the raw Vinted payload. Very verbose."),
) -> dict[str, Any]:
    data = await vinted.search(
        query=query,
        brand=brand,
        size=size,
        condition=condition,
        price_min=price_min,
        price_max=price_max,
        catalog=catalog,
        order=order,
        domain=domain,
        per_page=per_page,
        page=page,
        include_photo=include_photo,
        include_seller=include_seller,
        raw=raw,
    )
    if data.get("raw") is None:
        data.pop("raw", None)
    return {"ok": True, "data": data}


@app.get("/item/{item_id}", dependencies=[Depends(require_api_key)])
async def get_item(
    item_id: int = Path(..., ge=1),
    domain: str | None = Query(default=None),
    include_photo: bool = Query(default=False),
    include_seller: bool = Query(default=False),
    raw: bool = Query(default=False),
) -> dict[str, Any]:
    return {
        "ok": True,
        "data": await vinted.item(
            item_id,
            domain=domain,
            include_photo=include_photo,
            include_seller=include_seller,
            raw=raw,
        ),
    }


@app.get("/price-stats", dependencies=[Depends(require_api_key)])
async def price_stats(
    query: str | None = Query(default=None),
    brand: str | None = Query(default=None),
    size: str | None = Query(default=None),
    condition: str | None = Query(default=None),
    price_min: float | None = Query(default=None, ge=0),
    price_max: float | None = Query(default=None, ge=0),
    catalog: str | None = Query(default=None),
    order: str = Query(default="newest"),
    domain: str | None = Query(default=None),
    per_page: int = Query(default=96, ge=1, le=96),
) -> dict[str, Any]:
    stats = await vinted.price_stats(
        query=query,
        brand=brand,
        size=size,
        condition=condition,
        price_min=price_min,
        price_max=price_max,
        catalog=catalog,
        order=order,
        domain=domain,
        per_page=per_page,
    )
    return {"ok": True, "data": stats.model_dump()}
