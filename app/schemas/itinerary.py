# -*- coding: UTF-8 -*-
"""一日行程地图的聚合响应模型。"""

from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from app.integrations.maps.schemas import Coordinate, TravelMode

ItineraryMode = Literal["smart", "walking", "driving", "transit"]


class ItineraryLocation(BaseModel):
    id: Optional[int] = None
    label: str
    address: Optional[str] = None
    city: Optional[str] = None
    coordinate: Coordinate


class ItineraryItem(BaseModel):
    id: str
    kind: Literal["event", "task"]
    source_id: int
    schedule_id: Optional[int] = None
    title: str
    start_time: datetime
    end_time: datetime
    status: str
    event_type: Literal["activity", "travel"] = "activity"
    origin_location: Optional[ItineraryLocation] = None
    location: Optional[ItineraryLocation] = None


class ItineraryLeg(BaseModel):
    origin_id: str
    destination_id: str
    mode: TravelMode
    distance_m: Optional[int] = None
    duration_minutes: Optional[int] = None
    suggested_departure: Optional[datetime] = None
    estimated_arrival: Optional[datetime] = None
    available_minutes: Optional[int] = None
    route_available: bool = False
    path: List[Coordinate] = Field(default_factory=list)
    warning: Optional[str] = None
    parking_minutes: int = 0
    recommendation_reason: Optional[str] = None
    car_location_after: Optional[str] = None


class DailyItineraryResponse(BaseModel):
    date: date
    timezone: str
    mode: ItineraryMode
    include_return: bool = False
    car_available: bool = False
    parking_minutes: int = 10
    origin: Optional[ItineraryLocation] = None
    items: List[ItineraryItem] = Field(default_factory=list)
    legs: List[ItineraryLeg] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    map_message: Optional[str] = None
