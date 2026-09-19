# -*- coding: UTF-8 -*-
"""日程业务逻辑与用户偏好驱动的规划编排。"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.location import SavedLocation
from app.models.schedule import Schedule
from app.models.task import Task, TaskDependency
from app.integrations.maps.base import MapIntegrationError
from app.integrations.maps.service import get_map_service
from app.planning.engine import (
    Conflict,
    Evaluation,
    Interval,
    PlanAssignment,
    PlanResult,
    PlanTask,
    check_conflicts as engine_check_conflicts,
    evaluate_plan as engine_evaluate_plan,
    find_free_slots as engine_find_free_slots,
    generate_plan,
)
from app.planning import optimizer
from app.planning.windows import (
    build_local_work_windows,
    lunch_intervals,
    preferred_windows,
)
from app.schemas.preference import PreferenceSettings
from app.schemas.schedule import (
    PlanAnalysis,
    PlanRisk,
    PlanRiskReport,
    LocalReplanRequest,
    ScheduleGenerateRequest,
    ScheduleGenerateResponse,
    ScheduleMovePreview,
    ScheduleUpdate,
    UnscheduledTask,
)
from app.services import plan_version_service, preference_service, time_policy
from app.services.travel_planning_service import (
    LocatedBlock,
    PlanningLocation,
    TravelConstraintChecker,
)
from app.utils import to_naive_utc

IntervalTuple = Tuple[datetime, datetime]


@dataclass
class PlanningContext:
    start: datetime
    days: int
    windows: List[IntervalTuple]
    busy: List[IntervalTuple]
    tasks: List[PlanTask]
    scheduled_task_ids: set[int]
    completed_task_ids: set[int]
    dependency_completion_times: Dict[int, datetime]
    gap_minutes: int
    strategy: str
    travel_checker: TravelConstraintChecker
    requested_mode: str = "auto"
    selected_mode: str = "quick"
    selection_reason: str = ""
    fallback_reason: Optional[str] = None


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


def generate_schedules(
    db: Session,
    payload: ScheduleGenerateRequest,
) -> ScheduleGenerateResponse:
    context = _build_context(db, payload)
    result = _run_plan(context)
    analysis = _analysis(result, context)
    batch_id = uuid.uuid4().hex[:16]
    version = plan_version_service.begin_version(db, "generate", "自动生成任务安排")

    # 先完整计算，再只替换当前规划范围内的旧提议。
    scope_start, scope_end = _planning_scope(context)
    db.query(Schedule).filter(
        Schedule.status == "proposed",
        Schedule.is_locked.is_(False),
        Schedule.end_time > scope_start,
        Schedule.start_time < scope_end,
    ).delete(
        synchronize_session=False
    )
    created: List[Schedule] = []
    chunk_offsets = _existing_chunk_offsets(db)
    for assignment in result.assignments:
        schedule = Schedule(
            task_id=assignment.task_id,
            start_time=assignment.start_time,
            end_time=assignment.end_time,
            status="proposed",
            plan_batch_id=batch_id,
            plan_version_id=version.id,
            reason=assignment.reason,
            chunk_index=chunk_offsets.get(assignment.task_id, 0) + assignment.chunk_index,
            source="auto",
        )
        db.add(schedule)
        created.append(schedule)

    db.flush()
    plan_version_service.finalize_version(db, version)
    db.commit()
    for schedule in created:
        db.refresh(schedule)

    return ScheduleGenerateResponse(
        schedules=created,
        unscheduled=_unscheduled(result),
        analysis=analysis,
        batch_id=batch_id,
        version_id=version.id,
    )


def preview_plan(db: Session, payload: ScheduleGenerateRequest) -> dict:
    """计算规划预览，不改动数据库。"""

    context = _build_context(db, payload)
    result = _run_plan(context)
    scope_start, scope_end = _planning_scope(context)
    current = (
        db.query(Schedule)
        .filter(
            Schedule.status == "proposed",
            Schedule.is_locked.is_(False),
            Schedule.end_time > scope_start,
            Schedule.start_time < scope_end,
        )
        .order_by(Schedule.start_time, Schedule.id)
        .all()
    )
    preserved_count = (
        db.query(Schedule)
        .filter(
            Schedule.status.in_(("accepted", "proposed")),
            or_(Schedule.status == "accepted", Schedule.is_locked.is_(True)),
            Schedule.end_time > scope_start,
            Schedule.start_time < scope_end,
        )
        .count()
    )
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
        "scope": {
            "start": scope_start.isoformat() + "Z",
            "end": scope_end.isoformat() + "Z",
        },
        "comparison": _build_plan_comparison(
            current,
            result.assignments,
            preserved_count=preserved_count,
        ),
    }


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


def analyze_plan_risks(
    db: Session,
    start: datetime,
    end: datetime,
) -> PlanRiskReport:
    """Analyze only the requested planning range without modifying schedules."""

    start, end = _normalize_range(start, end)
    now = time_policy.utcnow()
    effective_start = max(start, now)
    prefs = preference_service.get_preferences(db)
    risks: List[PlanRisk] = []

    active_schedules = (
        db.query(Schedule)
        .filter(
            Schedule.status.in_(("accepted", "proposed")),
            Schedule.end_time > start,
            Schedule.start_time < end,
        )
        .order_by(Schedule.start_time, Schedule.id)
        .all()
    )
    events = (
        db.query(Event)
        .filter(Event.end_time > start, Event.start_time < end)
        .order_by(Event.start_time, Event.id)
        .all()
    )
    tasks = (
        db.query(Task)
        .filter(Task.status.in_(("pending", "in_progress")))
        .order_by(Task.deadline, Task.id)
        .all()
    )

    scheduled_minutes: Dict[int, int] = {}
    for schedule in db.query(Schedule).filter(
        Schedule.status.in_(("accepted", "proposed"))
    ):
        minutes = int((schedule.end_time - schedule.start_time).total_seconds() // 60)
        scheduled_minutes[schedule.task_id] = scheduled_minutes.get(schedule.task_id, 0) + minutes

    due_remaining: List[tuple[Task, int]] = []
    for task in tasks:
        deadline = to_naive_utc(task.deadline)
        if deadline is None or deadline > end:
            continue
        remaining = max(0, task.estimated_duration - scheduled_minutes.get(task.id, 0))
        if remaining <= 0:
            continue
        due_remaining.append((task, remaining))
        overdue = deadline <= now
        hours_left = max(0, int((deadline - now).total_seconds() // 3600))
        risks.append(
            PlanRisk(
                code="overdue_task" if overdue else "deadline_at_risk",
                severity="critical" if overdue or hours_left <= 24 else "warning",
                title=f"{task.title} 尚有 {remaining} 分钟未安排",
                detail=(
                    "截止时间已过，请调整截止时间或尽快补排"
                    if overdue
                    else f"距离截止约 {hours_left} 小时，建议纳入当前重排"
                ),
                task_ids=[task.id],
                action="replan",
            )
        )

    intervals: List[Interval] = []
    schedule_interval_ids: Dict[int, int] = {}
    for event in events:
        intervals.append(
            Interval(event.id, f"固定日程「{event.title}」", event.start_time, event.end_time)
        )
    for schedule in active_schedules:
        interval = Interval(
            -schedule.id,
            f"任务安排 #{schedule.id}",
            schedule.start_time,
            schedule.end_time,
        )
        intervals.append(interval)
        schedule_interval_ids[id(interval)] = schedule.id
    for conflict in engine_check_conflicts(intervals):
        schedule_ids = [
            schedule_interval_ids[item_id]
            for item_id in (id(conflict.first), id(conflict.second))
            if item_id in schedule_interval_ids
        ]
        risks.append(
            PlanRisk(
                code="time_conflict",
                severity="critical",
                title="当前范围存在时间冲突",
                detail=(
                    f"{conflict.first.title} 与 {conflict.second.title}"
                    f"重叠 {conflict.overlap_minutes} 分钟"
                ),
                schedule_ids=schedule_ids,
                action="replan" if schedule_ids else "review",
            )
        )

    blocks = sorted(
        [
            (max(start, item.start_time), min(end, item.end_time), item.title)
            for item in events
        ]
        + [
            (max(start, item.start_time), min(end, item.end_time), f"任务 #{item.task_id}")
            for item in active_schedules
        ],
        key=lambda item: (item[0], item[1]),
    )
    has_movable_schedule = any(not item.is_locked for item in active_schedules)
    _append_load_risks(risks, blocks, prefs, has_movable_schedule)

    if due_remaining and effective_start < end:
        days = max(1, (end.date() - effective_start.date()).days + 1)
        windows = [
            (max(window_start, effective_start), min(window_end, end))
            for window_start, window_end in build_local_work_windows(effective_start, days, prefs)
            if min(window_end, end) > max(window_start, effective_start)
        ]
        busy = [(item[0], item[1]) for item in blocks] + lunch_intervals(windows, prefs)
        free_slots = engine_find_free_slots(
            start=effective_start,
            days=days,
            busy_intervals=busy,
            windows=windows,
        )
        free_minutes = sum(int((slot_end - slot_start).total_seconds() // 60) for slot_start, slot_end in free_slots)
        required_minutes = sum(minutes for _, minutes in due_remaining)
        if required_minutes > free_minutes:
            risks.append(
                PlanRisk(
                    code="insufficient_capacity",
                    severity="critical",
                    title="截止任务超过当前可用时间",
                    detail=f"尚需 {required_minutes} 分钟，工作时段内仅剩约 {free_minutes} 分钟",
                    task_ids=[task.id for task, _ in due_remaining],
                    action="replan",
                )
            )

    counts = {
        severity: sum(item.severity == severity for item in risks)
        for severity in ("critical", "warning", "info")
    }
    health_score = max(0, 100 - counts["critical"] * 25 - counts["warning"] * 10 - counts["info"] * 3)
    can_replan = any(item.action == "replan" for item in risks)
    if not risks:
        recommendation = "当前范围没有发现明显风险"
    elif can_replan:
        recommendation = "建议先查看调整方案，确认后再应用"
    else:
        recommendation = "建议检查固定日程或适当降低当天负载"
    return PlanRiskReport(
        start=start,
        end=end,
        health_score=health_score,
        counts=counts,
        risks=risks,
        can_replan=can_replan,
        recommendation=recommendation,
    )


def _append_load_risks(
    risks: List[PlanRisk],
    blocks: List[tuple[datetime, datetime, str]],
    prefs: PreferenceSettings,
    has_movable_schedule: bool,
) -> None:
    zone = ZoneInfo(prefs.timezone)
    daily_minutes: Dict[object, int] = {}
    for block_start, block_end, _ in blocks:
        local_day = block_start.replace(tzinfo=timezone.utc).astimezone(zone).date()
        daily_minutes[local_day] = daily_minutes.get(local_day, 0) + int(
            (block_end - block_start).total_seconds() // 60
        )
    for day, minutes in daily_minutes.items():
        if minutes >= 10 * 60:
            risks.append(
                PlanRisk(
                    code="daily_overload",
                    severity="warning",
                    title=f"{day.month}月{day.day}日负载较高",
                    detail=(
                        f"已安排约 {minutes / 60:.1f} 小时，可查看调整方案"
                        if has_movable_schedule
                        else f"已安排约 {minutes / 60:.1f} 小时，当前均为固定日程，请人工确认强度"
                    ),
                    action="replan" if has_movable_schedule else "review",
                )
            )

    if not blocks:
        return
    chain_start, chain_end = blocks[0][0], blocks[0][1]
    chain_titles = [blocks[0][2]]
    minimum_break = timedelta(minutes=max(15, prefs.default_gap_minutes))
    for block_start, block_end, title in blocks[1:]:
        if block_start <= chain_end + minimum_break:
            chain_end = max(chain_end, block_end)
            chain_titles.append(title)
            continue
        _append_continuous_risk(
            risks, chain_start, chain_end, chain_titles, has_movable_schedule
        )
        chain_start, chain_end, chain_titles = block_start, block_end, [title]
    _append_continuous_risk(
        risks, chain_start, chain_end, chain_titles, has_movable_schedule
    )


def _append_continuous_risk(risks, start, end, titles, has_movable_schedule) -> None:
    minutes = int((end - start).total_seconds() // 60)
    if minutes < 4 * 60 or len(titles) < 2:
        return
    risks.append(
        PlanRisk(
            code="long_work_chain",
            severity="warning",
            title="连续安排时间较长",
            detail=(
                f"约 {minutes / 60:.1f} 小时缺少充分间隔，涉及 {len(titles)} 项安排"
                + ("，可尝试局部重排" if has_movable_schedule else "，且均为固定日程")
            ),
            action="replan" if has_movable_schedule else "review",
        )
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


def local_replan_preview(db: Session, payload: LocalReplanRequest) -> dict:
    movable, result, context = _prepare_local_replan(db, payload)
    return {
        "range": {
            "start": to_naive_utc(payload.start).isoformat() + "Z",
            "end": to_naive_utc(payload.end).isoformat() + "Z",
        },
        "kept_locked": _locked_schedule_count(db, payload),
        "removed": [_schedule_dict(item) for item in movable],
        "proposed": [_assignment_dict(item) for item in result.assignments],
        "unscheduled": [item.model_dump() for item in _unscheduled(result)],
        "analysis": _analysis(result, context).model_dump(),
        "comparison": _build_plan_comparison(
            movable,
            result.assignments,
            preserved_count=_locked_schedule_count(db, payload),
        ),
    }


def apply_local_replan(db: Session, payload: LocalReplanRequest) -> dict:
    movable, result, context = _prepare_local_replan(db, payload)
    original_task_ids = {item.task_id for item in movable}
    blocked = [item for item in result.failures if item.task_id in original_task_ids]
    if blocked:
        detail = "；".join(f"{item.title}：{item.reason}" for item in blocked)
        raise HTTPException(status_code=409, detail=f"局部重排无法保留原有任务：{detail}")
    if not movable and not result.assignments:
        return {"schedules": [], "version_id": None, "changes": []}

    original_status = {
        task_id: (
            "accepted"
            if any(item.task_id == task_id and item.status == "accepted" for item in movable)
            else "proposed"
        )
        for task_id in {item.task_id for item in movable}
    }
    version = plan_version_service.begin_version(db, "local_replan", "局部重排任务安排")
    movable_by_task: Dict[int, List[Schedule]] = {}
    for schedule in movable:
        movable_by_task.setdefault(schedule.task_id, []).append(schedule)
    assignments_by_task: Dict[int, List[PlanAssignment]] = {}
    for assignment in result.assignments:
        assignments_by_task.setdefault(assignment.task_id, []).append(assignment)

    movable_ids = {item.id for item in movable}
    offsets = _existing_chunk_offsets(db, exclude_schedule_ids=movable_ids)
    created: List[Schedule] = []
    for task_id in sorted(set(movable_by_task) | set(assignments_by_task)):
        existing = sorted(movable_by_task.get(task_id, []), key=lambda item: item.start_time)
        planned = sorted(assignments_by_task.get(task_id, []), key=lambda item: item.start_time)
        for schedule, assignment in zip(existing, planned):
            schedule.start_time = assignment.start_time
            schedule.end_time = assignment.end_time
            schedule.status = original_status.get(task_id, "proposed")
            schedule.reason = assignment.reason + " · 局部重排"
            schedule.plan_version_id = version.id
            schedule.chunk_index = offsets.get(task_id, 0) + assignment.chunk_index
            schedule.source = "replan"
            schedule.is_locked = False
            created.append(schedule)
        for schedule in existing[len(planned):]:
            db.delete(schedule)
        for assignment in planned[len(existing):]:
            schedule = Schedule(
                task_id=task_id,
                start_time=assignment.start_time,
                end_time=assignment.end_time,
                status=original_status.get(task_id, "proposed"),
                reason=assignment.reason + " · 局部重排",
                plan_batch_id=uuid.uuid4().hex[:16],
                plan_version_id=version.id,
                chunk_index=offsets.get(task_id, 0) + assignment.chunk_index,
                source="replan",
            )
            db.add(schedule)
            created.append(schedule)
    db.flush()
    changes = plan_version_service.finalize_version(db, version)
    db.commit()
    for schedule in created:
        db.refresh(schedule)
    return {
        "schedules": [_schedule_dict(item) for item in created],
        "version_id": version.id,
        "changes": changes,
    }


def list_plan_versions(db: Session, limit: int = 20) -> list[dict]:
    return plan_version_service.list_versions(db, limit)


def undo_latest_plan(db: Session) -> dict:
    return plan_version_service.undo_latest(db)


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


def audit_current_plan(
    db: Session,
    focus_event_ids: Optional[set[int]] = None,
    focus_schedule_ids: Optional[set[int]] = None,
) -> dict:
    """复查当前计划；给定范围时只报告与本次变更直接相关的冲突。"""

    scoped = focus_event_ids is not None or focus_schedule_ids is not None
    focus_event_ids = focus_event_ids or set()
    focus_schedule_ids = focus_schedule_ids or set()

    intervals: List[Interval] = []
    focused_intervals: set[int] = set()
    for event in db.query(Event).all():
        interval = Interval(
            event.id,
            f"事件：{event.title}",
            to_naive_utc(event.start_time),
            to_naive_utc(event.end_time),
        )
        intervals.append(interval)
        if event.id in focus_event_ids:
            focused_intervals.add(id(interval))
    for schedule in db.query(Schedule).filter(
        Schedule.status.in_(("accepted", "proposed"))
    ).all():
        interval = Interval(
            schedule.id,
            f"日程#{schedule.id}（任务#{schedule.task_id}）",
            to_naive_utc(schedule.start_time),
            to_naive_utc(schedule.end_time),
        )
        intervals.append(interval)
        if schedule.id in focus_schedule_ids:
            focused_intervals.add(id(interval))

    conflicts = engine_check_conflicts(intervals)
    if scoped:
        conflicts = [
            item
            for item in conflicts
            if id(item.first) in focused_intervals
            or id(item.second) in focused_intervals
        ]
    time_issues = [
        f"{item.first.title} 与 {item.second.title} 重叠 {item.overlap_minutes} 分钟"
        for item in conflicts
    ]
    locations = {item.id: item for item in db.query(SavedLocation).all()}
    try:
        map_service = get_map_service()
    except MapIntegrationError:
        map_service = None
    fixed_blocks = _fixed_travel_blocks(
        db,
        locations,
        schedule_statuses=("accepted", "proposed"),
    )
    checker = TravelConstraintChecker(
        map_service=map_service,
        fixed_blocks=fixed_blocks,
        task_locations=_task_locations(db, locations),
    )
    focus_keys = None
    if scoped:
        focus_keys = {
            *(f"event-{item_id}" for item_id in focus_event_ids),
            *(f"schedule-{item_id}" for item_id in focus_schedule_ids),
        }
    travel_issues = checker.sequence_issues([], focus_keys=focus_keys)
    return {
        "valid": not time_issues and not travel_issues,
        "issues": list(dict.fromkeys(time_issues + travel_issues)),
        "warnings": checker.warnings,
        "audited": bool(focus_keys) if scoped else True,
    }


def audit_applied_changes(db: Session, changes: List[dict]) -> dict:
    """从工具结果提取受影响记录，仅复查本次 Agent 操作的范围。"""

    event_ids: set[int] = set()
    schedule_ids: set[int] = set()
    task_ids: set[int] = set()

    for change in changes:
        name = str(change.get("name") or "")
        data = change.get("data")
        if not isinstance(data, dict):
            continue
        for event in data.get("events") or []:
            if isinstance(event, dict) and isinstance(event.get("id"), int):
                event_ids.add(event["id"])
        for schedule in data.get("schedules") or []:
            if isinstance(schedule, dict) and isinstance(schedule.get("id"), int):
                schedule_ids.add(schedule["id"])
        record_id = data.get("id")
        if isinstance(record_id, int):
            if "event" in name:
                event_ids.add(record_id)
            elif "schedule" in name:
                schedule_ids.add(record_id)
            elif "task" in name:
                task_ids.add(record_id)

    if task_ids:
        schedule_ids.update(
            item.id
            for item in db.query(Schedule)
            .filter(
                Schedule.task_id.in_(task_ids),
                Schedule.status.in_(("accepted", "proposed")),
            )
            .all()
        )
    return audit_current_plan(db, event_ids, schedule_ids)


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


def _build_context(
    db: Session,
    payload: ScheduleGenerateRequest,
    *,
    excluded_schedule_ids: Optional[set[int]] = None,
    task_ids: Optional[set[int]] = None,
) -> PlanningContext:
    start = time_policy.clamp_to_now(payload.start)
    prefs = preference_service.get_preferences(db)
    requested_windows = _payload_windows(payload)
    windows = (
        [
            (max(window_start, start), window_end)
            for window_start, window_end in requested_windows
            if window_end > start and max(window_start, start) < window_end
        ]
        if requested_windows
        else build_local_work_windows(start, payload.days, prefs)
    )
    if not windows:
        raise HTTPException(status_code=400, detail="规划时间窗口为空")

    excluded_schedule_ids = excluded_schedule_ids or set()
    accepted_query = db.query(Schedule).filter(
        or_(
            Schedule.status == "accepted",
            and_(Schedule.status == "proposed", Schedule.is_locked.is_(True)),
        )
    )
    if excluded_schedule_ids:
        accepted_query = accepted_query.filter(Schedule.id.notin_(excluded_schedule_ids))
    accepted = accepted_query.all()
    accepted_minutes: Dict[int, int] = {}
    completion_times: Dict[int, datetime] = {}
    for schedule in accepted:
        minutes = int((schedule.end_time - schedule.start_time).total_seconds() // 60)
        accepted_minutes[schedule.task_id] = accepted_minutes.get(schedule.task_id, 0) + minutes
        completion_times[schedule.task_id] = max(
            completion_times.get(schedule.task_id, schedule.end_time),
            schedule.end_time,
        )
    tasks_by_id = {task.id: task for task in db.query(Task).all()}
    completed_task_ids = {
        task_id
        for task_id, task in tasks_by_id.items()
        if task.status == "completed"
        or accepted_minutes.get(task_id, 0) >= task.estimated_duration
    }
    locations = {item.id: item for item in db.query(SavedLocation).all()}
    task_locations = _task_locations(db, locations)
    try:
        map_service = get_map_service()
    except MapIntegrationError:
        map_service = None
    travel_checker = TravelConstraintChecker(
        map_service=map_service,
        fixed_blocks=_fixed_travel_blocks(
            db,
            locations,
            exclude_schedule_ids=excluded_schedule_ids,
        ),
        task_locations=task_locations,
    )
    return PlanningContext(
        start=start,
        days=payload.days,
        windows=windows,
        busy=_busy_intervals(db, exclude_schedule_ids=excluded_schedule_ids)
        + lunch_intervals(windows, prefs),
        tasks=_plan_tasks(
            db,
            windows,
            prefs,
            accepted_minutes=accepted_minutes,
            task_ids=task_ids,
        ),
        scheduled_task_ids=completed_task_ids,
        completed_task_ids=completed_task_ids,
        dependency_completion_times=completion_times,
        gap_minutes=(
            payload.gap_minutes
            if payload.gap_minutes is not None
            else prefs.default_gap_minutes
        ),
        strategy=payload.strategy or prefs.planning_strategy,
        travel_checker=travel_checker,
        requested_mode=payload.planning_mode,
    )


def _run_plan(context: PlanningContext) -> PlanResult:
    try:
        selected_mode, reason = _select_planning_mode(context)
        context.selected_mode = selected_mode
        context.selection_reason = reason
        planner_kwargs = dict(
            start=context.start,
            tasks=context.tasks,
            busy_intervals=context.busy,
            scheduled_task_ids=context.scheduled_task_ids,
            completed_task_ids=context.completed_task_ids,
            dependency_completion_times=context.dependency_completion_times,
            windows=context.windows,
            gap_minutes=context.gap_minutes,
            strategy=context.strategy,
            candidate_validator=context.travel_checker.validate_candidate,
        )
        if selected_mode == "optimized":
            try:
                result = optimizer.generate_optimized_plan(**planner_kwargs)
            except RuntimeError as exc:
                context.selected_mode = "quick"
                context.fallback_reason = str(exc)
                context.selection_reason = "深度优化不可用，已自动切换为快速安排"
                result = generate_plan(days=context.days, **planner_kwargs)
        else:
            result = generate_plan(days=context.days, **planner_kwargs)
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


def _select_planning_mode(context: PlanningContext) -> tuple[str, str]:
    """Choose the planner explicitly or from observable request complexity."""

    if context.requested_mode == "quick":
        return "quick", "已按你的选择使用快速安排"
    if context.requested_mode == "optimized":
        return "optimized", "已按你的选择进行全局深度优化"

    tasks = context.tasks
    location_count = len(
        {
            location.key
            for task in tasks
            if (location := context.travel_checker.task_locations.get(task.id)) is not None
        }
    )
    deadline_count = sum(task.deadline is not None for task in tasks)
    dependency_count = sum(bool(task.dependency_ids) for task in tasks)
    span_days = max(
        1,
        (context.windows[-1][1].date() - context.windows[0][0].date()).days + 1,
    )
    reasons = []
    if len(tasks) >= 6:
        reasons.append(f"{len(tasks)} 项待安排任务")
    if span_days >= 3 and len(tasks) >= 3:
        reasons.append(f"规划范围跨 {span_days} 天")
    if deadline_count >= 2:
        reasons.append(f"{deadline_count} 个截止时间")
    if location_count >= 3:
        reasons.append(f"涉及 {location_count} 个地点")
    if dependency_count >= 2:
        reasons.append(f"{dependency_count} 项前置依赖")

    if reasons:
        return "optimized", "；".join(reasons)
    return "quick", "任务与约束较少，快速安排即可得到稳定结果"


def _plan_tasks(
    db: Session,
    planning_windows: List[IntervalTuple],
    prefs: PreferenceSettings,
    accepted_minutes: Optional[Dict[int, int]] = None,
    task_ids: Optional[set[int]] = None,
) -> List[PlanTask]:
    query = db.query(Task).filter(Task.status.in_(["pending", "in_progress"]))
    if task_ids is not None:
        query = query.filter(Task.id.in_(task_ids))
    rows = query.all()
    accepted_minutes = accepted_minutes or {}
    dependencies: Dict[int, List[int]] = {}
    for link in db.query(TaskDependency).all():
        dependencies.setdefault(link.task_id, []).append(link.depends_on_task_id)
    return [
        PlanTask(
            id=task.id,
            title=task.title,
            estimated_duration=max(
                0,
                task.estimated_duration - accepted_minutes.get(task.id, 0),
            ),
            priority=task.priority,
            start_time=to_naive_utc(task.start_time),
            earliest_start=to_naive_utc(task.earliest_start),
            deadline=to_naive_utc(task.deadline),
            created_at=task.created_at,
            preferred_windows=preferred_windows(task.preferred_time, planning_windows, prefs),
            splittable=task.splittable,
            min_chunk_minutes=task.min_chunk_minutes,
            max_chunk_minutes=task.max_chunk_minutes,
            dependency_ids=dependencies.get(task.id, []),
        )
        for task in rows
        if task.estimated_duration - accepted_minutes.get(task.id, 0) > 0
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
    schedule_statuses: Iterable[str] = ("accepted",),
    exclude_schedule_ids: Optional[set[int]] = None,
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
                    travel_mode=event.travel_mode,
                )
            )
    statuses = tuple(schedule_statuses)
    status_filter = Schedule.status.in_(statuses)
    if "proposed" not in statuses:
        status_filter = or_(
            status_filter,
            and_(Schedule.status == "proposed", Schedule.is_locked.is_(True)),
        )
    schedule_query = (
        db.query(Schedule, Task)
        .join(Task, Task.id == Schedule.task_id)
        .filter(status_filter)
    )
    if exclude_schedule_ids:
        schedule_query = schedule_query.filter(Schedule.id.notin_(exclude_schedule_ids))
    for schedule, task in schedule_query.all():
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


def _busy_intervals(
    db: Session,
    include_proposed: bool = False,
    exclude_schedule_ids: Optional[set[int]] = None,
) -> List[IntervalTuple]:
    busy = [
        (to_naive_utc(item.start_time), to_naive_utc(item.end_time))
        for item in db.query(Event).all()
    ]
    if include_proposed:
        status_filter = Schedule.status.in_(("accepted", "proposed"))
    else:
        status_filter = or_(
            Schedule.status == "accepted",
            and_(Schedule.status == "proposed", Schedule.is_locked.is_(True)),
        )
    schedule_query = db.query(Schedule).filter(status_filter)
    if exclude_schedule_ids:
        schedule_query = schedule_query.filter(Schedule.id.notin_(exclude_schedule_ids))
    busy.extend(
        (to_naive_utc(item.start_time), to_naive_utc(item.end_time))
        for item in schedule_query.all()
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
            time_text = _local_range_text(db, event.start_time, event.end_time)
            raise HTTPException(
                status_code=409,
                detail=f"该时间与固定日程「{event.title}」（{time_text}）冲突",
            )

    statuses = ("accepted", "proposed") if include_proposed else ("accepted",)
    query = (
        db.query(Schedule, Task)
        .join(Task, Task.id == Schedule.task_id)
        .filter(Schedule.status.in_(statuses))
    )
    if exclude_schedule_id is not None:
        query = query.filter(Schedule.id != exclude_schedule_id)
    status_labels = {"accepted": "已确认", "proposed": "待确认"}
    for schedule, task in query.all():
        if _overlaps(start, end, schedule.start_time, schedule.end_time):
            time_text = _local_range_text(db, schedule.start_time, schedule.end_time)
            status = status_labels.get(schedule.status, schedule.status)
            lock_text = "、已锁定" if schedule.is_locked else ""
            raise HTTPException(
                status_code=409,
                detail=(
                    f"该时间与任务安排「{task.title}」"
                    f"（#{schedule.id}，{time_text}，{status}{lock_text}）冲突"
                ),
            )


def _local_range_text(db: Session, start: datetime, end: datetime) -> str:
    timezone_name = preference_service.get_preferences(db).timezone
    zone = ZoneInfo(timezone_name)
    local_start = to_naive_utc(start).replace(tzinfo=timezone.utc).astimezone(zone)
    local_end = to_naive_utc(end).replace(tzinfo=timezone.utc).astimezone(zone)
    if local_start.date() == local_end.date():
        return (
            f"{local_start.month}月{local_start.day}日 "
            f"{local_start:%H:%M}–{local_end:%H:%M}"
        )
    return (
        f"{local_start.month}月{local_start.day}日 {local_start:%H:%M}–"
        f"{local_end.month}月{local_end.day}日 {local_end:%H:%M}"
    )


def _prepare_local_replan(
    db: Session,
    payload: LocalReplanRequest,
) -> tuple[List[Schedule], PlanResult, PlanningContext]:
    start, end = _normalize_range(payload.start, payload.end)
    start = time_policy.clamp_to_now(start)
    if end <= start:
        raise HTTPException(status_code=400, detail="局部重排时间范围已经结束")
    movable = (
        db.query(Schedule)
        .filter(
            Schedule.status.in_(("accepted", "proposed")),
            Schedule.is_locked.is_(False),
            Schedule.end_time > start,
            Schedule.start_time < end,
        )
        .order_by(Schedule.start_time)
        .all()
    )
    movable_ids = {item.id for item in movable}
    task_ids = {item.task_id for item in movable}
    if payload.include_unscheduled:
        scheduled_ids = {
            item.task_id
            for item in db.query(Schedule)
            .filter(Schedule.status.in_(("accepted", "proposed")))
            .all()
        }
        task_ids.update(
            item.id
            for item in db.query(Task)
            .filter(Task.status.in_(("pending", "in_progress")))
            .all()
            if item.id not in scheduled_ids
        )
    request = ScheduleGenerateRequest(
        start=start,
        days=max(1, (end.date() - start.date()).days + 1),
        windows=[{"start": start, "end": end}],
        gap_minutes=payload.gap_minutes,
        strategy=payload.strategy,
        planning_mode=payload.planning_mode,
    )
    context = _build_context(
        db,
        request,
        excluded_schedule_ids=movable_ids,
        task_ids=task_ids,
    )
    return movable, _run_plan(context), context


def _locked_schedule_count(db: Session, payload: LocalReplanRequest) -> int:
    start, end = _normalize_range(payload.start, payload.end)
    return (
        db.query(Schedule)
        .filter(
            Schedule.status.in_(("accepted", "proposed")),
            Schedule.is_locked.is_(True),
            Schedule.end_time > start,
            Schedule.start_time < end,
        )
        .count()
    )


def _existing_chunk_offsets(
    db: Session,
    exclude_schedule_ids: Optional[set[int]] = None,
) -> Dict[int, int]:
    offsets: Dict[int, int] = {}
    query = db.query(Schedule).filter(
        Schedule.status.in_(("accepted", "proposed"))
    )
    if exclude_schedule_ids:
        query = query.filter(Schedule.id.notin_(exclude_schedule_ids))
    for schedule in query:
        offsets[schedule.task_id] = max(
            offsets.get(schedule.task_id, 0),
            schedule.chunk_index or 1,
        )
    return offsets


def _schedule_dict(schedule: Schedule) -> dict:
    return {
        "id": schedule.id,
        "task_id": schedule.task_id,
        "start_time": schedule.start_time.isoformat() + "Z",
        "end_time": schedule.end_time.isoformat() + "Z",
        "status": schedule.status,
        "is_locked": schedule.is_locked,
        "chunk_index": schedule.chunk_index,
        "source": schedule.source,
        "reason": schedule.reason,
        "plan_version_id": schedule.plan_version_id,
    }


def _assignment_dict(assignment) -> dict:
    return {
        "task_id": assignment.task_id,
        "start_time": assignment.start_time.isoformat() + "Z",
        "end_time": assignment.end_time.isoformat() + "Z",
        "chunk_index": assignment.chunk_index,
        "reason": assignment.reason,
    }


def _planning_scope(context: PlanningContext) -> IntervalTuple:
    return (
        min(start for start, _ in context.windows),
        max(end for _, end in context.windows),
    )


def _build_plan_comparison(
    current: Iterable[Schedule],
    proposed: Iterable[PlanAssignment],
    *,
    preserved_count: int = 0,
) -> dict:
    """按任务和分段顺序对齐旧、新方案，生成可解释的差异。"""

    current_by_task: Dict[int, List[Schedule]] = {}
    for item in current:
        current_by_task.setdefault(item.task_id, []).append(item)
    proposed_by_task: Dict[int, List[PlanAssignment]] = {}
    for item in proposed:
        proposed_by_task.setdefault(item.task_id, []).append(item)

    changes = []
    counts = {"added": 0, "removed": 0, "moved": 0, "unchanged": 0}
    for task_id in sorted(set(current_by_task) | set(proposed_by_task)):
        before_items = sorted(
            current_by_task.get(task_id, []),
            key=lambda item: (item.start_time, item.end_time, item.id),
        )
        after_items = sorted(
            proposed_by_task.get(task_id, []),
            key=lambda item: (item.start_time, item.end_time, item.chunk_index),
        )
        for before, after in zip_longest(before_items, after_items):
            before_data = _schedule_dict(before) if before is not None else None
            after_data = _assignment_dict(after) if after is not None else None
            if before is None:
                change_type = "added"
            elif after is None:
                change_type = "removed"
            elif (
                before.start_time == after.start_time
                and before.end_time == after.end_time
            ):
                change_type = "unchanged"
            else:
                change_type = "moved"
            counts[change_type] += 1
            changes.append(
                {
                    "type": change_type,
                    "task_id": task_id,
                    "before": before_data,
                    "after": after_data,
                    "reason": after.reason if after is not None else "当前范围不再适合该安排",
                }
            )

    return {
        "counts": {**counts, "preserved": preserved_count},
        "changes": changes,
    }


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
    # A plan result should only describe the schedules it just produced. Full-calendar
    # conflict inspection remains available through the dedicated conflict checker.
    focus_keys = {f"task-{assignment.task_id}" for assignment in result.assignments}
    travel_issues = context.travel_checker.sequence_issues(
        result.assignments,
        focus_keys=focus_keys,
    )
    issues = evaluation.issues + travel_issues + context.travel_checker.warnings
    score = max(0, evaluation.score - 15 * len(travel_issues))
    return PlanAnalysis(
        score=score,
        scheduled_count=evaluation.scheduled_count,
        unscheduled_count=evaluation.unscheduled_count,
        scheduled_minutes=evaluation.scheduled_minutes,
        issues=list(dict.fromkeys(issues)),
        requested_mode=context.requested_mode,
        selected_mode=context.selected_mode,
        selection_reason=context.selection_reason,
        fallback_reason=context.fallback_reason,
    )


def _unscheduled(result: PlanResult) -> List[UnscheduledTask]:
    return [
        UnscheduledTask(id=item.task_id, title=item.title, reason=item.reason)
        for item in result.failures
    ]
