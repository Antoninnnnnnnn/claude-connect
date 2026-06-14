from datetime import datetime
from typing import Any

from fastapi import HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.centrale_client import SORT_PARAMS


CURRENT_YEAR = datetime.now().year


def effective_distance(distance_km: int | None, distance_bucket: int | None) -> int | None:
    if distance_bucket is not None:
        return distance_bucket
    return distance_km


def validate_ranges(
    price_min: float | None,
    price_max: float | None,
    year_min: int | None,
    year_max: int | None,
    mileage_min: int | None,
    mileage_max: int | None,
) -> None:
    if price_min is not None and price_max is not None and price_min > price_max:
        raise HTTPException(status_code=422, detail="price_min must be <= price_max")
    if year_min is not None and year_max is not None and year_min > year_max:
        raise HTTPException(status_code=422, detail="year_min must be <= year_max")
    if mileage_min is not None and mileage_max is not None and mileage_min > mileage_max:
        raise HTTPException(status_code=422, detail="mileage_min must be <= mileage_max")


class SearchFilters(BaseModel):
    make: str | None = None
    model: str | None = None
    version: str | None = None
    price_min: float | None = Field(default=None, ge=0)
    price_max: float | None = Field(default=None, ge=0)
    year_min: int | None = Field(default=None, ge=1900, le=CURRENT_YEAR + 1)
    year_max: int | None = Field(default=None, ge=1900, le=CURRENT_YEAR + 1)
    mileage_min: int | None = Field(default=None, ge=0)
    mileage_max: int | None = Field(default=None, ge=0)
    zip: str | None = Field(default=None, pattern=r"^\d{5}$")
    distance_km: int | None = Field(default=None, ge=0)
    distance_bucket: int | None = Field(default=None, ge=0)
    good_deal: str | None = None
    customer_family: str | None = None
    energy: str | None = None
    gearbox: str | None = None
    body_type: str | None = None
    color: str | None = None
    internal_color: str | None = None
    families: str | None = None
    options: str | None = None
    regions: str | None = None
    equipment_level: str | None = None
    critair: str | None = None
    co2_max: int | None = Field(default=None, ge=0)
    max_consumption: str | None = None
    doors: int | None = Field(default=None, ge=1)
    power: int | None = Field(default=None, ge=0)
    seats: int | None = Field(default=None, ge=1)
    four_wheel: bool | None = None
    freetext: str | None = None
    sort: str = "newest"
    page: int = Field(default=1, ge=1)
    url: str | None = None

    @field_validator("sort")
    @classmethod
    def check_sort(cls, value: str) -> str:
        lowered = value.lower()
        if lowered not in SORT_PARAMS:
            raise ValueError(f"Invalid sort: {value}")
        return lowered

    def client_kwargs(self) -> dict[str, Any]:
        distance = effective_distance(self.distance_km, self.distance_bucket)
        return {
            "make": self.make,
            "model": self.model,
            "version": self.version,
            "price_min": self.price_min,
            "price_max": self.price_max,
            "year_min": self.year_min,
            "year_max": self.year_max,
            "mileage_min": self.mileage_min,
            "mileage_max": self.mileage_max,
            "zip": self.zip,
            "distance_km": distance,
            "distance_bucket": distance,
            "good_deal": self.good_deal,
            "customer_family": self.customer_family,
            "energy": self.energy,
            "gearbox": self.gearbox,
            "body_type": self.body_type,
            "color": self.color,
            "internal_color": self.internal_color,
            "families": self.families,
            "options": self.options,
            "regions": self.regions,
            "equipment_level": self.equipment_level,
            "critair": self.critair,
            "co2_max": self.co2_max,
            "max_consumption": self.max_consumption,
            "doors": self.doors,
            "power": self.power,
            "seats": self.seats,
            "four_wheel": self.four_wheel,
            "freetext": self.freetext,
            "sort": self.sort,
            "page": self.page,
            "url": self.url,
        }


def search_filters_dependency(
    make: str | None = Query(default=None),
    model: str | None = Query(default=None),
    version: str | None = Query(default=None),
    price_min: float | None = Query(default=None, ge=0),
    price_max: float | None = Query(default=None, ge=0),
    year_min: int | None = Query(default=None, ge=1900, le=CURRENT_YEAR + 1),
    year_max: int | None = Query(default=None, ge=1900, le=CURRENT_YEAR + 1),
    mileage_min: int | None = Query(default=None, ge=0),
    mileage_max: int | None = Query(default=None, ge=0),
    zip: str | None = Query(default=None, pattern=r"^\d{5}$"),
    distance_km: int | None = Query(default=None, ge=0),
    distance_bucket: int | None = Query(default=None, ge=0),
    good_deal: str | None = Query(default=None),
    customer_family: str | None = Query(default=None),
    energy: str | None = Query(default=None),
    gearbox: str | None = Query(default=None),
    body_type: str | None = Query(default=None),
    color: str | None = Query(default=None),
    internal_color: str | None = Query(default=None),
    families: str | None = Query(default=None),
    options: str | None = Query(default=None),
    regions: str | None = Query(default=None),
    equipment_level: str | None = Query(default=None),
    critair: str | None = Query(default=None),
    co2_max: int | None = Query(default=None, ge=0),
    max_consumption: str | None = Query(default=None),
    doors: int | None = Query(default=None, ge=1),
    power: int | None = Query(default=None, ge=0),
    seats: int | None = Query(default=None, ge=1),
    four_wheel: bool | None = Query(default=None),
    freetext: str | None = Query(default=None),
    sort: str = Query(default="newest"),
    page: int = Query(default=1, ge=1),
    url: str | None = Query(default=None),
) -> SearchFilters:
    try:
        return SearchFilters(
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
            distance_km=distance_km,
            distance_bucket=distance_bucket,
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
            page=page,
            url=url,
        )
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
