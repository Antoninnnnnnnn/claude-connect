from typing import Any

from pydantic import BaseModel, Field


class SearchResult(BaseModel):
    id: int | None = None
    title: str | None = None
    body: str | None = None
    brand: str | None = None
    category_id: str | None = None
    category_name: str | None = None
    category_key: str | None = None
    category_path: list[str] | None = None
    ad_type: str | None = None
    status: str | None = None
    condition: str | None = None
    price: float | None = None
    old_price: float | None = None
    currency: str | None = "EUR"
    url: str | None = None
    image: str | None = None
    image_count: int | None = None
    images: list[str] | None = None
    first_publication_date: str | None = None
    index_date: str | None = None
    location: dict[str, Any] | None = None
    owner: dict[str, Any] | None = None
    seller_rating: dict[str, Any] | None = None
    shipping: dict[str, Any] | None = None
    options: dict[str, Any] | None = None
    attributes: list[dict[str, Any]] | None = None
    attributes_map: dict[str, Any] | None = None
    raw: dict[str, Any] | None = Field(default=None)


class PriceStats(BaseModel):
    count: int
    min: float | None = None
    max: float | None = None
    median: float | None = None
    currency: str | None = "EUR"
    sample_size: int
    failures: list[str] = Field(default_factory=list)
