from typing import Any

from pydantic import BaseModel, Field


class ApiResponse(BaseModel):
    ok: bool
    data: Any | None = None
    error: str | None = None


class SearchResult(BaseModel):
    id: int | None = None
    title: str | None = None
    brand: str | None = None
    size: str | None = None
    condition: str | None = None
    description: str | None = None
    category: str | None = None
    category_leaf: str | None = None
    categories: list[str] | None = None
    category_ids: list[int] | None = None
    color: str | None = None
    brand_id: int | None = None
    price: float | None = None
    currency: str | None = None
    total_item_price: float | None = None
    shipping_price: float | None = None
    shipping_text: str | None = None
    upload_date: str | None = None
    availability: str | None = None
    status_schema: str | None = None
    service_fee_included: bool | None = None
    url: str | None = None
    photo: str | None = None
    photos: list[str] | None = None
    seller: dict[str, Any] | None = None
    raw: dict[str, Any] | None = Field(default=None)


class PriceStats(BaseModel):
    count: int
    min: float | None = None
    max: float | None = None
    median: float | None = None
    currency: str | None = None
    sample_size: int
