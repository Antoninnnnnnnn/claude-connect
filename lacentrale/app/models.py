from typing import Any

from pydantic import BaseModel, Field


class SearchResult(BaseModel):
    id: str | None = None
    title: str | None = None
    make: str | None = None
    model: str | None = None
    version: str | None = None
    year: int | None = None
    mileage: int | None = None
    energy: str | None = None
    gearbox: str | None = None
    price: float | None = None
    currency: str | None = "EUR"
    location: dict[str, Any] | None = None
    dealer_type: str | None = None
    good_deal_badge: str | None = None
    url: str | None = None
    image: str | None = None
    description: str | None = None
    features: str | None = None
    equipment: str | None = None
    technical_sheet_url: str | None = None
    dealer: dict[str, Any] | None = None
    vehicle: dict[str, Any] | None = None
    raw: dict[str, Any] | None = Field(default=None)


class PriceStats(BaseModel):
    priced_count: int
    sample_size: int
    total: int | None = None
    min: float | None = None
    max: float | None = None
    median: float | None = None
    mean: float | None = None
    p25: float | None = None
    p75: float | None = None
    currency: str | None = "EUR"
    failures: list[str] = Field(default_factory=list)
    count: int
