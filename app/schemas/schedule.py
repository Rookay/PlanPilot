# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：schedule.py
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

ScheduleStatus = Literal["proposed", "accepted", "completed", "cancelled"]
PlanningStrategy = Literal["balanced", "deadline_first", "priority_first"]
PlanningMode = Literal["auto", "quick", "optimized"]


class ScheduleBase(BaseModel):
    task_id: int
    start_time: datetime
    end_time: datetime
    status: ScheduleStatus = "proposed"
    is_locked: bool = False
    chunk_index: int = Field(default=1, ge=1)
    source: str = Field(default="auto", max_length=20)

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
    is_locked: Optional[bool] = None


class ScheduleResponse(ScheduleBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
    plan_batch_id: Optional[str] = None
    plan_version_id: Optional[str] = None
    reason: Optional[str] = None


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
    gap_minutes: Optional[int] = Field(default=None, ge=0, le=240)
    #: 排序策略；缺省时读取用户偏好。
    strategy: Optional[PlanningStrategy] = None
    #: 规划引擎选择；auto 会按任务复杂度自动判断。
    planning_mode: PlanningMode = "auto"


class UnscheduledTask(BaseModel):
    id: int
    title: str
    reason: str


class PlanAnalysis(BaseModel):
    score: int = Field(ge=0, le=100)
    scheduled_count: int = 0
    unscheduled_count: int = 0
    scheduled_minutes: int = 0
    issues: List[str] = Field(default_factory=list)
    requested_mode: PlanningMode = "auto"
    selected_mode: Literal["quick", "optimized"] = "quick"
    selection_reason: str = ""
    fallback_reason: Optional[str] = None


class ScheduleGenerateResponse(BaseModel):
    schedules: List[ScheduleResponse]
    unscheduled: List[UnscheduledTask]
    analysis: PlanAnalysis
    batch_id: Optional[str] = None
    version_id: Optional[str] = None


class LocalReplanRequest(BaseModel):
    start: datetime
    end: datetime
    gap_minutes: Optional[int] = Field(default=None, ge=0, le=240)
    strategy: Optional[PlanningStrategy] = None
    planning_mode: PlanningMode = "auto"
    include_unscheduled: bool = False

    @model_validator(mode="after")
    def _check_range(self):
        if self.end <= self.start:
            raise ValueError("局部重排结束时间必须晚于开始时间")
        return self


class PlanVersionResponse(BaseModel):
    id: str
    action: str
    summary: str
    status: str
    changes: List[dict] = Field(default_factory=list)
    created_at: datetime
    undone_at: Optional[datetime] = None


class ScheduleMovePreview(BaseModel):
    valid: bool
    warnings: List[str] = Field(default_factory=list)
    conflicts: List[str] = Field(default_factory=list)
