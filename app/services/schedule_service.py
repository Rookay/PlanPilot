# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：schedule_service.py
"""日程业务逻辑。API 路由与未来的 Tool 共用。"""

from datetime import datetime, timedelta
from typing import Dict, List, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.schedule import Schedule
from app.models.task import Task
from app.planning.engine import (
    Conflict,
    Evaluation,
    Interval,
    PlanTask,
    check_conflicts as engine_check_conflicts,
    evaluate_plan as engine_evaluate_plan,
    find_free_slots as engine_find_free_slots,
    generate_plan,
)
from app.schemas.schedule import (
    ScheduleGenerateRequest,
    ScheduleGenerateResponse,
    ScheduleUpdate,
    UnscheduledTask,
)
from app.utils import to_naive_utc, utcnow


def _payload_windows(payload) -> List[Tuple[datetime, datetime]] | None:
    """把 payload.windows 转成 naive UTC 的 (start, end) 列表；无则返回 None。"""

    if not payload.windows:
        return None
    return [
        (to_naive_utc(window.start), to_naive_utc(window.end))
        for window in payload.windows
    ]


def list_schedules(db: Session) -> List[Schedule]:
    return db.query(Schedule).order_by(Schedule.start_time.asc()).all()


def create_schedule(
    db: Session,
    task_id: int,
    start_time: datetime,
    end_time: datetime,
    status: str = "proposed",
) -> Schedule:
    schedule = Schedule(
        task_id=task_id,
        start_time=start_time,
        end_time=end_time,
        status=status,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


def generate_schedules(
    db: Session,
    payload: ScheduleGenerateRequest,
) -> ScheduleGenerateResponse:
    start = to_naive_utc(payload.start) or utcnow()

    # 重新规划前先清掉上一次的「提议」日程；只有「已确认」的保留并占用时间。
    db.query(Schedule).filter(Schedule.status == "proposed").delete(
        synchronize_session=False
    )
    db.flush()

    busy: List[Tuple[datetime, datetime]] = []
    for event in db.query(Event).all():
        busy.append((to_naive_utc(event.start_time), to_naive_utc(event.end_time)))
    confirmed = db.query(Schedule).filter(Schedule.status == "accepted").all()
    for schedule in confirmed:
        busy.append(
            (to_naive_utc(schedule.start_time), to_naive_utc(schedule.end_time))
        )

    scheduled_task_ids = {schedule.task_id for schedule in confirmed}

    plan_tasks = [
        PlanTask(
            id=task.id,
            title=task.title,
            estimated_duration=task.estimated_duration,
            priority=task.priority,
            start_time=to_naive_utc(task.start_time),
            earliest_start=to_naive_utc(task.earliest_start),
            deadline=to_naive_utc(task.deadline),
            created_at=task.created_at,
        )
        for task in (
            db.query(Task)
            .filter(Task.status.in_(["pending", "in_progress"]))
            .all()
        )
    ]

    try:
        result = generate_plan(
            start=start,
            days=payload.days,
            tasks=plan_tasks,
            busy_intervals=busy,
            scheduled_task_ids=scheduled_task_ids,
            windows=_payload_windows(payload),
            gap_minutes=payload.gap_minutes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    created: List[Schedule] = []
    for assignment in result.assignments:
        schedule = Schedule(
            task_id=assignment.task_id,
            start_time=assignment.start_time,
            end_time=assignment.end_time,
            status="proposed",
        )
        db.add(schedule)
        created.append(schedule)

    db.commit()
    for schedule in created:
        db.refresh(schedule)

    unscheduled = [
        UnscheduledTask(id=failure.task_id, title=failure.title, reason=failure.reason)
        for failure in result.failures
    ]

    return ScheduleGenerateResponse(schedules=created, unscheduled=unscheduled)


def clear_schedules(db: Session) -> dict:
    """清空全部自动生成的日程提议。"""

    deleted = (
        db.query(Schedule)
        .filter(Schedule.status == "proposed")
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"deleted": deleted}


def update_schedule(db: Session, schedule_id: int, payload: ScheduleUpdate) -> Schedule:
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
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


def delete_schedule(db: Session, schedule_id: int) -> dict:
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")

    db.delete(schedule)
    db.commit()
    return {"message": "Schedule deleted", "id": schedule_id}


def find_free_slots(
    db: Session,
    start: datetime,
    days: int,
    min_duration_minutes: int = 30,
) -> List[Tuple[datetime, datetime]]:
    """查询某时间范围内的空闲时间段。"""

    busy = _busy_intervals(db, include_proposed=True)

    return engine_find_free_slots(
        start=to_naive_utc(start),
        days=days,
        busy_intervals=busy,
        min_duration=timedelta(minutes=min_duration_minutes),
    )


def _busy_intervals(db: Session, include_proposed: bool = False) -> List[Tuple[datetime, datetime]]:
    """汇总事件与有效日程的忙碌区间。

    cancelled / completed 不占用时间；默认只算 accepted（已确认），
    include_proposed=True 时把 proposed（待确认）也算作占用。
    """

    busy: List[Tuple[datetime, datetime]] = []
    for event in db.query(Event).all():
        busy.append((to_naive_utc(event.start_time), to_naive_utc(event.end_time)))

    statuses = ("accepted", "proposed") if include_proposed else ("accepted",)
    for schedule in db.query(Schedule).filter(Schedule.status.in_(statuses)).all():
        busy.append(
            (to_naive_utc(schedule.start_time), to_naive_utc(schedule.end_time))
        )
    return busy


def _plan_tasks(db: Session) -> List[PlanTask]:
    """把待安排任务转成规划引擎输入。"""

    return [
        PlanTask(
            id=task.id,
            title=task.title,
            estimated_duration=task.estimated_duration,
            priority=task.priority,
            start_time=to_naive_utc(task.start_time),
            earliest_start=to_naive_utc(task.earliest_start),
            deadline=to_naive_utc(task.deadline),
            created_at=task.created_at,
        )
        for task in (
            db.query(Task)
            .filter(Task.status.in_(["pending", "in_progress"]))
            .all()
        )
    ]


def replan_preview(
    db: Session,
    payload: ScheduleGenerateRequest,
) -> dict:
    """计算重新规划后的变化（dry-run，不落库）。

    保留 accepted/completed/cancelled 日程，只重新计算 proposed 部分，
    返回「将被移除的 proposed」与「新生成的 proposed」用于确认展示。
    """

    start = to_naive_utc(payload.start) or utcnow()

    current_proposed = (
        db.query(Schedule).filter(Schedule.status == "proposed").all()
    )

    busy = _busy_intervals(db)
    scheduled_task_ids = {
        schedule.task_id
        for schedule in db.query(Schedule).filter(Schedule.status == "accepted").all()
    }

    try:
        result = generate_plan(
            start=start,
            days=payload.days,
            tasks=_plan_tasks(db),
            busy_intervals=busy,
            scheduled_task_ids=scheduled_task_ids,
            windows=_payload_windows(payload),
            gap_minutes=payload.gap_minutes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "removed": [
            {
                "task_id": schedule.task_id,
                "start_time": schedule.start_time.isoformat(),
                "end_time": schedule.end_time.isoformat(),
            }
            for schedule in current_proposed
        ],
        "proposed": [
            {
                "task_id": assignment.task_id,
                "start_time": assignment.start_time.isoformat(),
                "end_time": assignment.end_time.isoformat(),
            }
            for assignment in result.assignments
        ],
        "unscheduled": [
            {"task_id": failure.task_id, "title": failure.title, "reason": failure.reason}
            for failure in result.failures
        ],
    }


def check_conflicts(db: Session) -> List[Conflict]:
    """检测事件与日程之间的时间冲突。"""

    intervals: List[Interval] = []
    for event in db.query(Event).all():
        intervals.append(
            Interval(
                id=event.id,
                title=event.title,
                start=to_naive_utc(event.start_time),
                end=to_naive_utc(event.end_time),
            )
        )
    for schedule in db.query(Schedule).filter(
        Schedule.status.in_(("accepted", "proposed"))
    ).all():
        intervals.append(
            Interval(
                id=schedule.id,
                title=f"日程#{schedule.id}（任务#{schedule.task_id}）",
                start=to_naive_utc(schedule.start_time),
                end=to_naive_utc(schedule.end_time),
            )
        )

    return engine_check_conflicts(intervals)


def evaluate_plan_dry_run(
    db: Session,
    payload: ScheduleGenerateRequest,
) -> Evaluation:
    """干跑一次规划并评估质量（不落库）。"""

    start = to_naive_utc(payload.start) or utcnow()

    busy = _busy_intervals(db)
    scheduled_task_ids = {
        schedule.task_id
        for schedule in db.query(Schedule).filter(Schedule.status == "accepted").all()
    }

    tasks = _plan_tasks(db)
    result = generate_plan(
        start=start,
        days=payload.days,
        tasks=tasks,
        busy_intervals=busy,
        scheduled_task_ids=scheduled_task_ids,
        windows=_payload_windows(payload),
        gap_minutes=payload.gap_minutes,
    )

    tasks_by_id = {task.id: task for task in tasks}
    return engine_evaluate_plan(result.assignments, result.failures, tasks_by_id)
