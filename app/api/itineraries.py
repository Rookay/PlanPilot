# -*- coding: UTF-8 -*-
"""一日行程地图接口。"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.itinerary import (
    DailyItineraryResponse,
    ItineraryMode,
    ItineraryOriginStrategy,
)
from app.services import itinerary_service

router = APIRouter(prefix="/api/itineraries", tags=["Itineraries"])


@router.get("/day", response_model=DailyItineraryResponse)
def daily_itinerary(
    date_value: date = Query(alias="date"),
    mode: ItineraryMode = Query(default="smart"),
    include_proposed: bool = Query(default=True),
    include_return: bool = Query(default=False),
    car_available: bool = Query(default=False),
    parking_minutes: int = Query(default=10, ge=0, le=60),
    origin_strategy: ItineraryOriginStrategy = Query(default="automatic"),
    origin_location_id: int | None = Query(default=None, ge=1),
    destination_location_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
):
    return itinerary_service.get_daily_itinerary(
        db,
        date_value,
        mode,
        include_proposed,
        include_return,
        car_available,
        parking_minutes,
        origin_strategy,
        origin_location_id,
        destination_location_id,
    )
