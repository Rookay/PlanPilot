# -*- coding: UTF-8 -*-
"""用户规划偏好的输入输出模型。"""

from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

PlanningStrategy = Literal["balanced", "deadline_first", "priority_first"]


class PreferenceSettings(BaseModel):
    timezone: str = "Asia/Shanghai"
    default_location: str = Field(default="上海", min_length=1, max_length=100)
    workday_start: str = "09:00"
    workday_end: str = "18:00"
    lunch_start: Optional[str] = "12:00"
    lunch_end: Optional[str] = "13:00"
    default_task_duration_min: int = Field(default=60, ge=5, le=24 * 60)
    default_gap_minutes: int = Field(default=15, ge=0, le=240)
    planning_strategy: PlanningStrategy = "balanced"
    schedule_weekends: bool = True

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("请输入有效的 IANA 时区，例如 Asia/Shanghai") from exc
        return value

    @field_validator("workday_start", "workday_end", "lunch_start", "lunch_end")
    @classmethod
    def _validate_clock(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return None
        try:
            hour_text, minute_text = value.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        except (ValueError, AttributeError) as exc:
            raise ValueError("时间必须使用 HH:MM 格式") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("时间必须使用 HH:MM 格式")
        return f"{hour:02d}:{minute:02d}"

    @model_validator(mode="after")
    def _validate_ranges(self):
        if self.workday_end <= self.workday_start:
            raise ValueError("工作结束时间必须晚于开始时间")
        if bool(self.lunch_start) != bool(self.lunch_end):
            raise ValueError("午休开始和结束时间需要同时填写")
        if self.lunch_start and self.lunch_end and self.lunch_end <= self.lunch_start:
            raise ValueError("午休结束时间必须晚于开始时间")
        return self


class PreferenceUpdate(BaseModel):
    timezone: Optional[str] = None
    default_location: Optional[str] = Field(default=None, min_length=1, max_length=100)
    workday_start: Optional[str] = None
    workday_end: Optional[str] = None
    lunch_start: Optional[str] = None
    lunch_end: Optional[str] = None
    default_task_duration_min: Optional[int] = Field(default=None, ge=5, le=24 * 60)
    default_gap_minutes: Optional[int] = Field(default=None, ge=0, le=240)
    planning_strategy: Optional[PlanningStrategy] = None
    schedule_weekends: Optional[bool] = None
