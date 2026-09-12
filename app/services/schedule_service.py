# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：schedule_service.py
"""日程业务逻辑。API 路由与未来的 Tool 共用。"""

from datetime import datetime
from typing import Dict, List, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.schedule import Schedule
from app.models.task import Task
from app.planning.engine import PlanTask, generate_plan
from app.schemas.schedule import (
    ScheduleGenerateRequest,
    ScheduleGenerateResponse,
    ScheduleUpdate,
    UnscheduledTask,
)
from app.utils import to_naive_utc, utcnow


def list_schedules(db: Session) -> List[Schedule]:
    return db.query(Schedule).order_by(Schedule.start_time.asc()).all()


def generate_schedules(
    db: Session,
    payload: ScheduleGenerateRequest,
) -> ScheduleGenerateResponse:
    start = to_naive_utc(payload.start) or utcnow()

    # 重新规划前先清掉上一次的「提议」日程，已确认/已完成的保留。
    db.query(Schedule).filter(Schedule.status == "proposed").delete(
        synchronize_session=False
    )
    db.flush()

    busy: List[Tuple[datetime, datetime]] = []
    for event in db.query(Event).all():
        busy.append((to_naive_utc(event.start_time), to_naive_utc(event.end_time)))
    for schedule in db.query(Schedule).all():
        busy.append(
            (to_naive_utc(schedule.start_time), to_naive_utc(schedule.end_time))
        )

    scheduled_task_ids = {schedule.task_id for schedule in db.query(Schedule).all()}

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
