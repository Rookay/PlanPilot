# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：task.py
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TaskStatus = Literal["pending", "in_progress", "completed", "cancelled"]
TaskPriority = Literal["low", "medium", "high"]


class TaskBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)

    status: TaskStatus = "pending"
    priority: TaskPriority = "medium"

    estimated_duration: int = Field(default=60, ge=1, le=24 * 60)

    start_time: Optional[datetime] = None

    deadline: Optional[datetime] = None
    earliest_start: Optional[datetime] = None

    preferred_time: Optional[str] = Field(default=None, max_length=20)
    category: Optional[str] = Field(default=None, max_length=50)
    location_id: Optional[int] = Field(default=None, ge=1)
    location: Optional[str] = Field(default=None, max_length=200)
    location_city: Optional[str] = Field(default=None, max_length=100)
    location_longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    location_latitude: Optional[float] = Field(default=None, ge=-90, le=90)

    @field_validator("title", mode="before")
    @classmethod
    def _strip_title(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator(
        "description",
        "preferred_time",
        "category",
        "location",
        "location_city",
        mode="before",
    )
    @classmethod
    def _empty_string_to_none(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def _check_time_constraints(self):
        if self.deadline and self.earliest_start and self.earliest_start > self.deadline:
            raise ValueError("最早可开始时间不能晚于截止时间")
        if self.deadline and self.start_time and self.start_time > self.deadline:
            raise ValueError("开始时间不能晚于截止时间")
        if (self.location_longitude is None) != (self.location_latitude is None):
            raise ValueError("地点经度和纬度必须同时提供")
        return self


class TaskCreate(TaskBase):
    pass


class TaskUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)

    status: Optional[TaskStatus] = None
    priority: Optional[TaskPriority] = None

    estimated_duration: Optional[int] = Field(
        default=None,
        ge=1,
        le=24 * 60,
    )

    start_time: Optional[datetime] = None

    deadline: Optional[datetime] = None
    earliest_start: Optional[datetime] = None

    preferred_time: Optional[str] = Field(default=None, max_length=20)
    category: Optional[str] = Field(default=None, max_length=50)
    location_id: Optional[int] = Field(default=None, ge=1)
    location: Optional[str] = Field(default=None, max_length=200)
    location_city: Optional[str] = Field(default=None, max_length=100)
    location_longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    location_latitude: Optional[float] = Field(default=None, ge=-90, le=90)

    @field_validator("title", mode="before")
    @classmethod
    def _strip_title(cls, value):
        return value.strip() if isinstance(value, str) else value


class TaskResponse(TaskBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
