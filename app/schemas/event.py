# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：event.py
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EventType = Literal["activity", "travel"]
TravelMode = Literal["walking", "driving", "taxi", "transit"]


class EventBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)

    start_time: datetime
    end_time: datetime
    event_type: EventType = "activity"
    travel_mode: Optional[TravelMode] = None

    location: Optional[str] = Field(default=None, max_length=200)
    location_id: Optional[int] = Field(default=None, ge=1)
    location_city: Optional[str] = Field(default=None, max_length=100)
    location_longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    location_latitude: Optional[float] = Field(default=None, ge=-90, le=90)

    origin_location: Optional[str] = Field(default=None, max_length=200)
    origin_location_id: Optional[int] = Field(default=None, ge=1)
    origin_location_city: Optional[str] = Field(default=None, max_length=100)
    origin_location_longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    origin_location_latitude: Optional[float] = Field(default=None, ge=-90, le=90)

    @field_validator("title", mode="before")
    @classmethod
    def _strip_title(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator(
        "description",
        "location",
        "location_city",
        "origin_location",
        "origin_location_city",
        mode="before",
    )
    @classmethod
    def _empty_string_to_none(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def _check_time_range(self):
        if self.end_time <= self.start_time:
            raise ValueError("结束时间必须晚于开始时间")
        if (self.location_longitude is None) != (self.location_latitude is None):
            raise ValueError("地点经度和纬度必须同时提供")
        if (self.origin_location_longitude is None) != (
            self.origin_location_latitude is None
        ):
            raise ValueError("出发地经度和纬度必须同时提供")
        if self.event_type == "travel" and self.travel_mode is None:
            self.travel_mode = "transit"
        if self.event_type != "travel":
            self.travel_mode = None
        return self


class EventCreate(EventBase):
    pass


class EventUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)

    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    event_type: Optional[EventType] = None
    travel_mode: Optional[TravelMode] = None

    location: Optional[str] = Field(default=None, max_length=200)
    location_id: Optional[int] = Field(default=None, ge=1)
    location_city: Optional[str] = Field(default=None, max_length=100)
    location_longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    location_latitude: Optional[float] = Field(default=None, ge=-90, le=90)

    origin_location: Optional[str] = Field(default=None, max_length=200)
    origin_location_id: Optional[int] = Field(default=None, ge=1)
    origin_location_city: Optional[str] = Field(default=None, max_length=100)
    origin_location_longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    origin_location_latitude: Optional[float] = Field(default=None, ge=-90, le=90)

    @field_validator("title", mode="before")
    @classmethod
    def _strip_title(cls, value):
        return value.strip() if isinstance(value, str) else value


class EventResponse(EventBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trip_id: Optional[str] = None
    trip_title: Optional[str] = None
    trip_order: Optional[int] = None
    created_at: datetime


class TripEventUpdate(EventUpdate):
    event_id: int = Field(ge=1)


class TripRevision(BaseModel):
    trip_id: str = Field(min_length=1, max_length=32)
    shift_minutes: int = Field(default=0, ge=-1440, le=1440)
    updates: List[TripEventUpdate] = Field(default_factory=list, max_length=30)
    delete_event_ids: List[int] = Field(default_factory=list, max_length=30)
    trip_title: Optional[str] = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _has_change(self):
        if (
            self.shift_minutes == 0
            and not self.updates
            and not self.delete_event_ids
            and self.trip_title is None
        ):
            raise ValueError("至少需要提供一项行程修改")
        update_ids = [item.event_id for item in self.updates]
        if len(update_ids) != len(set(update_ids)):
            raise ValueError("同一事件只能在一次行程修订中更新一次")
        return self


class TripSummary(BaseModel):
    trip_id: str
    title: str
    start_time: datetime
    end_time: datetime
    event_count: int
    cities: List[str] = Field(default_factory=list)


class EventSeriesCreate(EventBase):
    recurrence: Literal["daily", "weekdays", "weekly"]
    occurrences: int = Field(default=2, ge=2, le=52)


class EventSeriesResponse(BaseModel):
    events: List[EventResponse]
    created: int
