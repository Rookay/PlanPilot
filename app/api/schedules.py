# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：schedules.py
"""日程规划接口。

提供一个简单但实用的自动排期算法：
- 只在每天的工作时间段（默认 09:00 - 22:00）内安排任务；
- 自动避开已有事件（Event）和其他日程（Schedule）；
- 尊重任务的 ``earliest_start`` / ``deadline``；
- 按「截止时间优先、优先级次之」的顺序贪心放置。
"""

from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.event import Event
from app.models.schedule import Schedule
from app.models.task import Task
from app.schemas.schedule import (
    ScheduleGenerateRequest,
    ScheduleGenerateResponse,
    ScheduleResponse,
    ScheduleUpdate,
    UnscheduledTask,
)
from app.utils import to_naive_utc, utcnow

router = APIRouter(
    prefix="/api/schedules",
    tags=["Schedules"],
)

# 时间粒度（分钟）：所有自动生成的日程都会对齐到该粒度。
SLOT_MINUTES = 15

# 默认允许安排任务的时间段。
DEFAULT_DAY_START = time(9, 0)
DEFAULT_DAY_END = time(22, 0)

# 优先级排序权重，数值越小越先安排。
PRIORITY_WEIGHT = {"high": 0, "medium": 1, "low": 2}


def _ceil_to_slot(moment: datetime) -> datetime:
    """把时间向上取整到下一个 SLOT_MINUTES 边界。"""

    moment = moment.replace(second=0, microsecond=0)
    remainder = moment.minute % SLOT_MINUTES
    if remainder:
        moment += timedelta(minutes=SLOT_MINUTES - remainder)
    return moment


def _merge_intervals(
    intervals: List[Tuple[datetime, datetime]],
) -> List[Tuple[datetime, datetime]]:
    """合并重叠的时间区间，便于快速判断空闲时段。"""

    if not intervals:
        return []

    ordered = sorted(intervals, key=lambda item: item[0])
    merged = [ordered[0]]

    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    return merged


def _build_windows(
    start: datetime,
    days: int,
    day_start: time,
    day_end: time,
) -> List[Tuple[datetime, datetime]]:
    """生成每天的可工作窗口，并裁掉起始时间之前的部分。"""

    windows: List[Tuple[datetime, datetime]] = []
    first_day = start.date()

    for offset in range(days):
        day = first_day + timedelta(days=offset)
        window_start = max(datetime.combine(day, day_start), start)
        window_end = datetime.combine(day, day_end)
        if window_end > window_start:
            windows.append((window_start, window_end))

    return windows


def _find_free_slot(
    windows: List[Tuple[datetime, datetime]],
    busy: List[Tuple[datetime, datetime]],
    duration: timedelta,
    earliest: datetime,
    deadline: Optional[datetime],
) -> Optional[datetime]:
    """在窗口内寻找第一个足够长的空闲时段。"""

    for window_start, window_end in windows:
        cursor = _ceil_to_slot(max(window_start, earliest))
        if cursor >= window_end:
            continue

        for busy_start, busy_end in busy:
            if busy_end <= cursor:
                continue
            if busy_start >= window_end:
                break
            if cursor + duration <= busy_start:
                break
            cursor = _ceil_to_slot(max(cursor, busy_end))
            if cursor >= window_end:
                break

        if cursor + duration <= window_end:
            if deadline is not None and cursor + duration > deadline:
                # 窗口按时间递增，后面的窗口只会更晚，直接判定无法安排。
                return None
            return cursor

    return None


def _sort_key(task: Task):
    """排序规则：先按开始时间，再按截止时间、优先级、创建时间。"""

    start = to_naive_utc(task.start_time) or to_naive_utc(task.earliest_start)
    deadline = to_naive_utc(task.deadline)
    return (
        start is None,
        start or datetime.max,
        deadline is None,
        deadline or datetime.max,
        PRIORITY_WEIGHT.get(task.priority, 1),
        task.created_at or datetime.min,
    )


@router.get("", response_model=List[ScheduleResponse])
def list_schedules(db: Session = Depends(get_db)):
    return (
        db.query(Schedule)
        .order_by(Schedule.start_time.asc())
        .all()
    )


@router.post("/generate", response_model=ScheduleGenerateResponse)
def generate_schedules(
    payload: ScheduleGenerateRequest,
    db: Session = Depends(get_db),
):
    start = to_naive_utc(payload.start) or utcnow()
    windows = _build_windows(
        start=start,
        days=payload.days,
        day_start=DEFAULT_DAY_START,
        day_end=DEFAULT_DAY_END,
    )

    if not windows:
        raise HTTPException(status_code=400, detail="规划时间窗口为空")

    horizon_start = windows[0][0]
    horizon_end = windows[-1][1]

    busy: List[Tuple[datetime, datetime]] = []

    for event in db.query(Event).all():
        event_start = to_naive_utc(event.start_time)
        event_end = to_naive_utc(event.end_time)
        if event_end > horizon_start and event_start < horizon_end:
            busy.append((event_start, event_end))

    # 重新规划前先清掉上一次的「提议」日程，已确认/已完成的保留。
    db.query(Schedule).filter(Schedule.status == "proposed").delete(
        synchronize_session=False
    )
    db.flush()

    for schedule in db.query(Schedule).all():
        schedule_start = to_naive_utc(schedule.start_time)
        schedule_end = to_naive_utc(schedule.end_time)
        if schedule_end > horizon_start and schedule_start < horizon_end:
            busy.append((schedule_start, schedule_end))

    busy = _merge_intervals(busy)

    tasks = (
        db.query(Task)
        .filter(Task.status.in_(["pending", "in_progress"]))
        .all()
    )
    tasks.sort(key=_sort_key)

    unscheduled: List[UnscheduledTask] = []
    created: List[Schedule] = []
    scheduled_task_ids = {schedule.task_id for schedule in db.query(Schedule).all()}

    for task in tasks:
        if task.id in scheduled_task_ids:
            continue

        duration = timedelta(minutes=max(task.estimated_duration or 60, 1))
        earliest = (
            to_naive_utc(task.start_time)
            or to_naive_utc(task.earliest_start)
            or horizon_start
        )
        deadline = to_naive_utc(task.deadline)

        if deadline is not None and deadline <= start:
            unscheduled.append(
                UnscheduledTask(
                    id=task.id,
                    title=task.title,
                    reason="截止时间早于规划起始时间",
                )
            )
            continue

        slot_start = _find_free_slot(
            windows=windows,
            busy=busy,
            duration=duration,
            earliest=max(earliest, horizon_start),
            deadline=deadline,
        )

        if slot_start is None:
            unscheduled.append(
                UnscheduledTask(
                    id=task.id,
                    title=task.title,
                    reason="在给定时间范围内找不到足够长的空闲时段",
                )
            )
            continue

        schedule = Schedule(
            task_id=task.id,
            start_time=slot_start,
            end_time=slot_start + duration,
            status="proposed",
        )
        db.add(schedule)
        created.append(schedule)
        busy = _merge_intervals(busy + [(slot_start, slot_start + duration)])

    db.commit()
    for schedule in created:
        db.refresh(schedule)

    return ScheduleGenerateResponse(schedules=created, unscheduled=unscheduled)


@router.delete("")
def clear_schedules(db: Session = Depends(get_db)):
    """清空全部自动生成的日程提议。"""

    deleted = (
        db.query(Schedule)
        .filter(Schedule.status == "proposed")
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"deleted": deleted}


@router.patch("/{schedule_id}", response_model=ScheduleResponse)
def update_schedule(
    schedule_id: int,
    payload: ScheduleUpdate,
    db: Session = Depends(get_db),
):
    schedule = (
        db.query(Schedule)
        .filter(Schedule.id == schedule_id)
        .first()
    )
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")

    update_data: Dict = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(schedule, key, value)

    if schedule.end_time <= schedule.start_time:
        raise HTTPException(status_code=400, detail="结束时间必须晚于开始时间")

    db.commit()
    db.refresh(schedule)
    return schedule


@router.delete("/{schedule_id}")
def delete_schedule(
    schedule_id: int,
    db: Session = Depends(get_db),
):
    schedule = (
        db.query(Schedule)
        .filter(Schedule.id == schedule_id)
        .first()
    )
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")

    db.delete(schedule)
    db.commit()
    return {"message": "Schedule deleted", "id": schedule_id}
