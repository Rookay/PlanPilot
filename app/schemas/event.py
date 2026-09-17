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
    created_at: datetime


class EventSeriesCreate(EventBase):
    recurrence: Literal["daily", "weekdays", "weekly"]
    occurrences: int = Field(default=2, ge=2, le=52)


class EventSeriesResponse(BaseModel):
    events: List[EventResponse]
    created: int
