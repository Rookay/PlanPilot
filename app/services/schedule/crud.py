# -*- coding: UTF-8 -*-
"""日程的增删改查、批量接受与冲突检查。

"日程"实体的直接操作层，不涉及规划。写入前统一走 ``_ensure_slot_available``
校验，避免绕过规划器手工造出时间重叠。
"""

from datetime import datetime, timedelta
from typing import Dict, List

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.location import SavedLocation
from app.models.schedule import Schedule
from app.models.task import Task
from app.integrations.maps.base import MapIntegrationError
from app.integrations.maps.service import get_map_service
from app.planning.engine import (
    Conflict,
    Interval,
    PlanTask,
    check_conflicts as engine_check_conflicts,
    find_free_slots as engine_find_free_slots,
)
from app.planning.windows import (
    build_local_work_windows,
    lunch_intervals,
)
from app.schemas.schedule import (
    ScheduleMovePreview,
    ScheduleUpdate,
)
from app.services import plan_version_service, preference_service, time_policy
from app.services.travel_planning_service import (
    TravelConstraintChecker,
)
from app.utils import to_naive_utc

from app.services.schedule.core import (
    IntervalTuple,
    _busy_intervals,
    _ensure_slot_available,
    _fixed_travel_blocks,
    _normalize_range,
    _schedule_dict,
    _task_locations,
)



def list_schedules(db: Session) -> List[Schedule]:
    return db.query(Schedule).order_by(Schedule.start_time.asc()).all()


def create_schedule(
    db: Session,
    task_id: int,
    start_time: datetime,
    end_time: datetime,
    status: str = "proposed",
) -> Schedule:
    task = db.query(Task).filter(Task.id == task_id).first()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    start_time, end_time = _normalize_range(start_time, end_time)
    time_policy.ensure_not_past(start_time)
    _ensure_slot_available(db, start_time, end_time)
    version = plan_version_service.begin_version(db, "manual_create", "手动创建任务安排")
    schedule = Schedule(
        task_id=task_id,
        start_time=start_time,
        end_time=end_time,
        status=status,
        reason="手动安排",
        source="manual",
        plan_version_id=version.id,
    )
    db.add(schedule)
    db.flush()
    plan_version_service.finalize_version(db, version)
    db.commit()
    db.refresh(schedule)
    return schedule


def accept_all_schedules(db: Session, batch_id: str | None = None) -> dict:
    query = db.query(Schedule).filter(Schedule.status == "proposed")
    if batch_id:
        query = query.filter(Schedule.plan_batch_id == batch_id)
    schedules = query.order_by(Schedule.start_time).all()

    for schedule in schedules:
        time_policy.ensure_not_past(schedule.start_time)
        _ensure_slot_available(
            db,
            schedule.start_time,
            schedule.end_time,
            exclude_schedule_id=schedule.id,
            include_proposed=False,
        )
    if not schedules:
        return {"accepted": 0, "batch_id": batch_id, "version_id": None}
    version = plan_version_service.begin_version(db, "accept", "确认任务安排")
    for schedule in schedules:
        schedule.status = "accepted"
        schedule.plan_version_id = version.id
    db.flush()
    plan_version_service.finalize_version(db, version)
    db.commit()
    return {"accepted": len(schedules), "batch_id": batch_id, "version_id": version.id}


def clear_schedules(db: Session) -> dict:
    count = db.query(Schedule).filter(
        Schedule.status == "proposed",
        Schedule.is_locked.is_(False),
    ).count()
    if not count:
        return {"deleted": 0, "version_id": None}
    version = plan_version_service.begin_version(db, "clear", "清空待确认安排")
    deleted = (
        db.query(Schedule)
        .filter(Schedule.status == "proposed", Schedule.is_locked.is_(False))
        .delete(synchronize_session=False)
    )
    db.flush()
    plan_version_service.finalize_version(db, version)
    db.commit()
    return {"deleted": deleted, "version_id": version.id}


def update_schedule(db: Session, schedule_id: int, payload: ScheduleUpdate) -> Schedule:
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")

    update_data: Dict = payload.model_dump(exclude_unset=True)
    start, end = _normalize_range(
        update_data.get("start_time", schedule.start_time),
        update_data.get("end_time", schedule.end_time),
    )
    next_status = update_data.get("status", schedule.status)
    changes_time = start != schedule.start_time or end != schedule.end_time
    if changes_time or (
        next_status in {"proposed", "accepted"} and next_status != schedule.status
    ):
        time_policy.ensure_not_past(start)
    if schedule.is_locked and changes_time:
        raise HTTPException(status_code=409, detail="该日程已锁定，请先解锁再调整时间")
    if next_status in {"proposed", "accepted"}:
        _ensure_slot_available(db, start, end, exclude_schedule_id=schedule_id)
        if changes_time:
            travel_preview = preview_schedule_update(db, schedule_id, payload)
            if not travel_preview.valid:
                raise HTTPException(
                    status_code=409,
                    detail="；".join(travel_preview.conflicts),
                )

    version = plan_version_service.begin_version(db, "update", "修改任务安排")
    update_data.update({"start_time": start, "end_time": end})
    for key, value in update_data.items():
        setattr(schedule, key, value)
    schedule.plan_version_id = version.id
    if changes_time:
        schedule.source = "drag" if schedule.source != "manual" else "manual"

    if next_status == "completed":
        task = db.query(Task).filter(Task.id == schedule.task_id).first()
        if task is not None:
            task.status = "completed"
    db.flush()
    plan_version_service.finalize_version(db, version)
    db.commit()
    db.refresh(schedule)
    return schedule


def delete_schedule(db: Session, schedule_id: int) -> dict:
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    version = plan_version_service.begin_version(db, "delete", "移除任务安排")
    db.delete(schedule)
    db.flush()
    plan_version_service.finalize_version(db, version)
    db.commit()
    return {"message": "Schedule deleted", "id": schedule_id, "version_id": version.id}


def delete_task_schedules(db: Session, task_id: int) -> dict:
    """Remove all active schedule blocks for one task as one undoable change."""

    task = db.query(Task).filter(Task.id == task_id).first()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    schedules = (
        db.query(Schedule)
        .filter(
            Schedule.task_id == task_id,
            Schedule.status.in_(("accepted", "proposed")),
        )
        .order_by(Schedule.start_time, Schedule.id)
        .all()
    )
    if not schedules:
        raise HTTPException(status_code=404, detail="该任务没有可移除的有效安排")
    version = plan_version_service.begin_version(
        db,
        "delete_task_schedules",
        f"移除任务「{task.title}」的全部安排",
    )
    ids = [schedule.id for schedule in schedules]
    for schedule in schedules:
        db.delete(schedule)
    db.flush()
    plan_version_service.finalize_version(db, version)
    db.commit()
    return {
        "message": "Task schedules deleted",
        "task_id": task_id,
        "task_title": task.title,
        "ids": ids,
        "deleted": len(ids),
        "version_id": version.id,
    }


def find_free_slots(
    db: Session,
    start: datetime,
    days: int,
    min_duration_minutes: int = 30,
) -> List[IntervalTuple]:
    prefs = preference_service.get_preferences(db)
    start = time_policy.clamp_to_now(start)
    windows = build_local_work_windows(start, days, prefs)
    busy = _busy_intervals(db, include_proposed=True) + lunch_intervals(windows, prefs)
    return engine_find_free_slots(
        start=start,
        days=days,
        busy_intervals=busy,
        min_duration=timedelta(minutes=min_duration_minutes),
        windows=windows,
    )


def preview_schedule_update(
    db: Session,
    schedule_id: int,
    payload: ScheduleUpdate,
) -> ScheduleMovePreview:
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    update_data = payload.model_dump(exclude_unset=True)
    start, end = _normalize_range(
        update_data.get("start_time", schedule.start_time),
        update_data.get("end_time", schedule.end_time),
    )
    conflicts: List[str] = []
    try:
        time_policy.ensure_not_past(start)
        _ensure_slot_available(db, start, end, exclude_schedule_id=schedule_id)
    except HTTPException as exc:
        conflicts.append(str(exc.detail))

    locations = {item.id: item for item in db.query(SavedLocation).all()}
    try:
        map_service = get_map_service()
    except MapIntegrationError:
        map_service = None
    checker = TravelConstraintChecker(
        map_service=map_service,
        fixed_blocks=_fixed_travel_blocks(
            db,
            locations,
            schedule_statuses=("accepted", "proposed"),
            exclude_schedule_ids={schedule_id},
        ),
        task_locations=_task_locations(db, locations),
    )
    candidate = PlanTask(id=schedule.task_id, title=f"任务 #{schedule.task_id}")
    if not checker.validate_candidate(candidate, start, end, []):
        reason = checker.rejection_reasons.get(schedule.task_id)
        if reason:
            conflicts.append(reason)
    return ScheduleMovePreview(
        valid=not conflicts,
        conflicts=list(dict.fromkeys(conflicts)),
        warnings=checker.warnings,
    )


def check_conflicts(db: Session) -> List[Conflict]:
    intervals: List[Interval] = []
    for event in db.query(Event).all():
        intervals.append(
            Interval(
                event.id,
                f"事件：{event.title}",
                to_naive_utc(event.start_time),
                to_naive_utc(event.end_time),
            )
        )
    for schedule in db.query(Schedule).filter(
        Schedule.status.in_(("accepted", "proposed"))
    ).all():
        intervals.append(
            Interval(
                schedule.id,
                f"日程#{schedule.id}（任务#{schedule.task_id}）",
                to_naive_utc(schedule.start_time),
                to_naive_utc(schedule.end_time),
            )
        )
    return engine_check_conflicts(intervals)
