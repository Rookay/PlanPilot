# -*- coding: UTF-8 -*-
"""日程业务逻辑与用户偏好驱动的规划编排。"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

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
    Evaluation,
    Interval,
    PlanResult,
    PlanTask,
    check_conflicts as engine_check_conflicts,
    evaluate_plan as engine_evaluate_plan,
    find_free_slots as engine_find_free_slots,
    generate_plan,
)
from app.planning.windows import (
    build_local_work_windows,
    lunch_intervals,
    preferred_windows,
)
from app.schemas.preference import PreferenceSettings
from app.schemas.schedule import (
    PlanAnalysis,
    ScheduleGenerateRequest,
    ScheduleGenerateResponse,
    ScheduleUpdate,
    UnscheduledTask,
)
from app.services import preference_service
from app.services.travel_planning_service import (
    LocatedBlock,
    PlanningLocation,
    TravelConstraintChecker,
)
from app.utils import to_naive_utc, utcnow

IntervalTuple = Tuple[datetime, datetime]


@dataclass
class PlanningContext:
    start: datetime
    days: int
    windows: List[IntervalTuple]
    busy: List[IntervalTuple]
    tasks: List[PlanTask]
    scheduled_task_ids: set[int]
    gap_minutes: int
    strategy: str
    travel_checker: TravelConstraintChecker


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
    _ensure_slot_available(db, start_time, end_time)
    schedule = Schedule(
        task_id=task_id,
        start_time=start_time,
        end_time=end_time,
        status=status,
        reason="手动安排",
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


def generate_schedules(
    db: Session,
    payload: ScheduleGenerateRequest,
) -> ScheduleGenerateResponse:
    context = _build_context(db, payload)
    result = _run_plan(context)
    analysis = _analysis(result, context)
    batch_id = uuid.uuid4().hex[:16]

    # 先完整计算，再替换旧提议，避免计算失败时丢失已有计划。
    db.query(Schedule).filter(Schedule.status == "proposed").delete(
        synchronize_session=False
    )
    created: List[Schedule] = []
    for assignment in result.assignments:
        schedule = Schedule(
            task_id=assignment.task_id,
            start_time=assignment.start_time,
            end_time=assignment.end_time,
            status="proposed",
            plan_batch_id=batch_id,
            reason=assignment.reason,
        )
        db.add(schedule)
        created.append(schedule)

    db.commit()
    for schedule in created:
        db.refresh(schedule)

    return ScheduleGenerateResponse(
        schedules=created,
        unscheduled=_unscheduled(result),
        analysis=analysis,
        batch_id=batch_id,
    )


def preview_plan(db: Session, payload: ScheduleGenerateRequest) -> dict:
    """计算规划预览，不改动数据库。"""

    context = _build_context(db, payload)
    result = _run_plan(context)
    return {
        "schedules": [
            {
                "task_id": item.task_id,
                "start_time": item.start_time.isoformat() + "Z",
                "end_time": item.end_time.isoformat() + "Z",
                "reason": item.reason,
            }
            for item in result.assignments
        ],
        "unscheduled": [item.model_dump() for item in _unscheduled(result)],
        "analysis": _analysis(result, context).model_dump(),
    }


def accept_all_schedules(db: Session, batch_id: str | None = None) -> dict:
    query = db.query(Schedule).filter(Schedule.status == "proposed")
    if batch_id:
        query = query.filter(Schedule.plan_batch_id == batch_id)
    schedules = query.order_by(Schedule.start_time).all()

    for schedule in schedules:
        _ensure_slot_available(
            db,
            schedule.start_time,
            schedule.end_time,
            exclude_schedule_id=schedule.id,
            include_proposed=False,
        )
    for schedule in schedules:
        schedule.status = "accepted"
    db.commit()
    return {"accepted": len(schedules), "batch_id": batch_id}


def clear_schedules(db: Session) -> dict:
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
    start, end = _normalize_range(
        update_data.get("start_time", schedule.start_time),
        update_data.get("end_time", schedule.end_time),
    )
    next_status = update_data.get("status", schedule.status)
    if next_status in {"proposed", "accepted"}:
        _ensure_slot_available(db, start, end, exclude_schedule_id=schedule_id)

    update_data.update({"start_time": start, "end_time": end})
    for key, value in update_data.items():
        setattr(schedule, key, value)

    if next_status == "completed":
        task = db.query(Task).filter(Task.id == schedule.task_id).first()
        if task is not None:
            task.status = "completed"
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
) -> List[IntervalTuple]:
    prefs = preference_service.get_preferences(db)
    start = to_naive_utc(start)
    windows = build_local_work_windows(start, days, prefs)
    busy = _busy_intervals(db, include_proposed=True) + lunch_intervals(windows, prefs)
    return engine_find_free_slots(
        start=start,
        days=days,
        busy_intervals=busy,
        min_duration=timedelta(minutes=min_duration_minutes),
        windows=windows,
    )


def replan_preview(db: Session, payload: ScheduleGenerateRequest) -> dict:
    current = db.query(Schedule).filter(Schedule.status == "proposed").all()
    preview = preview_plan(db, payload)
    preview["removed"] = [
        {
            "task_id": item.task_id,
            "start_time": item.start_time.isoformat() + "Z",
            "end_time": item.end_time.isoformat() + "Z",
        }
        for item in current
    ]
    preview["proposed"] = preview.pop("schedules")
    return preview


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


def evaluate_plan_dry_run(db: Session, payload: ScheduleGenerateRequest) -> Evaluation:
    context = _build_context(db, payload)
    result = _run_plan(context)
    analysis = _analysis(result, context)
    return Evaluation(
        score=analysis.score,
        issues=analysis.issues,
        scheduled_count=analysis.scheduled_count,
        unscheduled_count=analysis.unscheduled_count,
        scheduled_minutes=analysis.scheduled_minutes,
    )


def _build_context(db: Session, payload: ScheduleGenerateRequest) -> PlanningContext:
    start = to_naive_utc(payload.start) or utcnow()
    prefs = preference_service.get_preferences(db)
    windows = _payload_windows(payload) or build_local_work_windows(start, payload.days, prefs)
    if not windows:
        raise HTTPException(status_code=400, detail="规划时间窗口为空")

    accepted = db.query(Schedule).filter(Schedule.status == "accepted").all()
    locations = {item.id: item for item in db.query(SavedLocation).all()}
    task_locations = _task_locations(db, locations)
    try:
        map_service = get_map_service()
    except MapIntegrationError:
        map_service = None
    travel_checker = TravelConstraintChecker(
        map_service=map_service,
        fixed_blocks=_fixed_travel_blocks(db, locations),
        task_locations=task_locations,
    )
    return PlanningContext(
        start=start,
        days=payload.days,
        windows=windows,
        busy=_busy_intervals(db) + lunch_intervals(windows, prefs),
        tasks=_plan_tasks(db, windows, prefs),
        scheduled_task_ids={item.task_id for item in accepted},
        gap_minutes=(
            payload.gap_minutes
            if payload.gap_minutes is not None
            else prefs.default_gap_minutes
        ),
        strategy=payload.strategy or prefs.planning_strategy,
        travel_checker=travel_checker,
    )


def _run_plan(context: PlanningContext) -> PlanResult:
    try:
        result = generate_plan(
            start=context.start,
            days=context.days,
            tasks=context.tasks,
            busy_intervals=context.busy,
            scheduled_task_ids=context.scheduled_task_ids,
            windows=context.windows,
            gap_minutes=context.gap_minutes,
            strategy=context.strategy,
            candidate_validator=context.travel_checker.validate_candidate,
        )
        for failure in result.failures:
            reason = context.travel_checker.rejection_reasons.get(failure.task_id)
            if reason:
                failure.reason = f"没有满足通勤约束的可用时段：{reason}"
        for assignment in result.assignments:
            if assignment.task_id in context.travel_checker.validated_task_ids:
                assignment.reason += " · 已用高德校验通勤"
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _plan_tasks(
    db: Session,
    planning_windows: List[IntervalTuple],
    prefs: PreferenceSettings,
) -> List[PlanTask]:
    rows = db.query(Task).filter(Task.status.in_(["pending", "in_progress"])).all()
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
            preferred_windows=preferred_windows(task.preferred_time, planning_windows, prefs),
        )
        for task in rows
    ]


def _task_locations(
    db: Session,
    saved_locations: Dict[int, SavedLocation],
) -> Dict[int, PlanningLocation]:
    result: Dict[int, PlanningLocation] = {}
    for task in db.query(Task).all():
        location = _planning_location(task, saved_locations)
        if location is not None:
            result[task.id] = location
    return result


def _fixed_travel_blocks(
    db: Session,
    saved_locations: Dict[int, SavedLocation],
) -> List[LocatedBlock]:
    blocks: List[LocatedBlock] = []
    for event in db.query(Event).all():
        location = _planning_location(event, saved_locations)
        if location is not None:
            blocks.append(
                LocatedBlock(
                    key=f"event-{event.id}",
                    title=event.title,
                    start=to_naive_utc(event.start_time),
                    end=to_naive_utc(event.end_time),
                    location=location,
                    travel=event.event_type == "travel",
                )
            )
    accepted = (
        db.query(Schedule, Task)
        .join(Task, Task.id == Schedule.task_id)
        .filter(Schedule.status == "accepted")
        .all()
    )
    for schedule, task in accepted:
        location = _planning_location(task, saved_locations)
        if location is not None:
            blocks.append(
                LocatedBlock(
                    key=f"schedule-{schedule.id}",
                    title=task.title,
                    start=to_naive_utc(schedule.start_time),
                    end=to_naive_utc(schedule.end_time),
                    location=location,
                )
            )
    return blocks


def _planning_location(
    item,
    saved_locations: Dict[int, SavedLocation],
) -> PlanningLocation | None:
    saved = saved_locations.get(item.location_id)
    if saved is not None:
        return PlanningLocation(
            label=saved.label,
            city=saved.city,
            longitude=saved.longitude,
            latitude=saved.latitude,
        )
    if item.location_longitude is None or item.location_latitude is None:
        return None
    return PlanningLocation(
        label=item.location or item.title,
        city=item.location_city,
        longitude=item.location_longitude,
        latitude=item.location_latitude,
    )


def _payload_windows(payload: ScheduleGenerateRequest) -> List[IntervalTuple] | None:
    if not payload.windows:
        return None
    return sorted(
        (to_naive_utc(window.start), to_naive_utc(window.end))
        for window in payload.windows
    )


def _busy_intervals(db: Session, include_proposed: bool = False) -> List[IntervalTuple]:
    busy = [
        (to_naive_utc(item.start_time), to_naive_utc(item.end_time))
        for item in db.query(Event).all()
    ]
    statuses = ("accepted", "proposed") if include_proposed else ("accepted",)
    busy.extend(
        (to_naive_utc(item.start_time), to_naive_utc(item.end_time))
        for item in db.query(Schedule).filter(Schedule.status.in_(statuses)).all()
    )
    return busy


def _ensure_slot_available(
    db: Session,
    start: datetime,
    end: datetime,
    exclude_schedule_id: int | None = None,
    include_proposed: bool = True,
) -> None:
    for event in db.query(Event).all():
        if _overlaps(start, end, event.start_time, event.end_time):
            raise HTTPException(status_code=409, detail=f"该时间与事件「{event.title}」冲突")

    statuses = ("accepted", "proposed") if include_proposed else ("accepted",)
    query = db.query(Schedule).filter(Schedule.status.in_(statuses))
    if exclude_schedule_id is not None:
        query = query.filter(Schedule.id != exclude_schedule_id)
    for schedule in query.all():
        if _overlaps(start, end, schedule.start_time, schedule.end_time):
            raise HTTPException(status_code=409, detail=f"该时间与日程 #{schedule.id} 冲突")


def _normalize_range(start: datetime, end: datetime) -> IntervalTuple:
    start, end = to_naive_utc(start), to_naive_utc(end)
    if start is None or end is None or end <= start:
        raise HTTPException(status_code=400, detail="结束时间必须晚于开始时间")
    return start, end


def _overlaps(first_start, first_end, second_start, second_end) -> bool:
    return first_start < to_naive_utc(second_end) and to_naive_utc(second_start) < first_end


def _analysis(result: PlanResult, context: PlanningContext) -> PlanAnalysis:
    evaluation = engine_evaluate_plan(
        result.assignments,
        result.failures,
        {task.id: task for task in context.tasks},
    )
    travel_issues = context.travel_checker.sequence_issues(result.assignments)
    issues = evaluation.issues + travel_issues + context.travel_checker.warnings
    score = max(0, evaluation.score - 15 * len(travel_issues))
    return PlanAnalysis(
        score=score,
        scheduled_count=evaluation.scheduled_count,
        unscheduled_count=evaluation.unscheduled_count,
        scheduled_minutes=evaluation.scheduled_minutes,
        issues=list(dict.fromkeys(issues)),
    )


def _unscheduled(result: PlanResult) -> List[UnscheduledTask]:
    return [
        UnscheduledTask(id=item.task_id, title=item.title, reason=item.reason)
        for item in result.failures
    ]
