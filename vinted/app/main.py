from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.vinted_client import VintedClient, VintedError

app = FastAPI(
    title="Self-hosted Vinted Search API",
    version="1.0.0",
    docs_url="/docs",
)

settings = get_settings()
vinted = VintedClient(settings)


@app.on_event("startup")
async def startup() -> None:
    await vinted.start()
    await vinted.refresh_session(settings.default_domain)


@app.on_event("shutdown")
async def shutdown() -> None:
    await vinted.close()


@app.exception_handler(VintedError)
async def vinted_error_handler(_: Request, exc: VintedError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"ok": False, "error": str(exc)})


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": str(exc.detail)})


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    current_settings: Settings = Depends(get_settings),
) -> None:
    if not current_settings.api_key:
        raise HTTPException(status_code=500, detail="API_KEY is not configured")
    if x_api_key != current_settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "data": {"status": "up"}}


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
