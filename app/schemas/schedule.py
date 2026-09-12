# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：schedule.py
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

ScheduleStatus = Literal["proposed", "accepted", "completed", "cancelled"]


class ScheduleBase(BaseModel):
    task_id: int
    start_time: datetime
    end_time: datetime
    status: ScheduleStatus = "proposed"

    @model_validator(mode="after")
    def _check_time_range(self):
        if self.end_time <= self.start_time:
            raise ValueError("结束时间必须晚于开始时间")
        return self


class ScheduleCreate(ScheduleBase):
    pass


class ScheduleUpdate(BaseModel):
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    status: Optional[ScheduleStatus] = None


class ScheduleResponse(ScheduleBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class ScheduleWindow(BaseModel):
    """一个允许安排任务的时间窗口。"""

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _check_time_range(self):
        if self.end <= self.start:
            raise ValueError("结束时间必须晚于开始时间")
        return self


class ScheduleGenerateRequest(BaseModel):
    """自动规划参数。"""

    #: 规划起始时间，缺省为当前时间（UTC）。
    start: Optional[datetime] = None
    #: 规划天数。
    days: int = Field(default=7, ge=1, le=30)
    #: 显式时间窗口（例如「今天晚上 + 明天下午」）；传入后优先于 start/days。
    windows: Optional[List[ScheduleWindow]] = None
    #: 相邻任务之间的最小空闲间隔（分钟）。
    gap_minutes: int = Field(default=0, ge=0, le=240)


class UnscheduledTask(BaseModel):
    id: int
    title: str
    reason: str


class ScheduleGenerateResponse(BaseModel):
    schedules: List[ScheduleResponse]
    unscheduled: List[UnscheduledTask]
