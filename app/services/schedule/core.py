# -*- coding: UTF-8 -*-
"""日程规划的基础设施与执行。

三部分：时间与单位换算的纯工具、把数据库状态翻译成引擎输入的
``_build_context``、调用引擎并解释其选择与降级的 ``_run_plan`` 一族。
本模块不依赖任何业务簇，因此不会出现循环导入。
"""

from dataclasses import dataclass
from datetime import datetime, timezone
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
    PlanResult,
    PlanTask,
    evaluate_plan as engine_evaluate_plan,
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
    ScheduleGenerateRequest,
    UnscheduledTask,
)
from app.services import preference_service, time_policy
from app.services.travel_planning_service import (
    LocatedBlock,
    PlanningLocation,
    TravelConstraintChecker,
)
from app.utils import to_naive_utc

from app.services.schedule.comparison import _schedule_dict


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
    timezone_name: str = "Asia/Shanghai"
    requested_mode: str = "auto"
    selected_mode: str = "quick"
    selection_reason: str = ""
    fallback_reason: Optional[str] = None


def _normalize_range(start: datetime, end: datetime) -> IntervalTuple:
    start, end = to_naive_utc(start), to_naive_utc(end)
    if start is None or end is None or end <= start:
        raise HTTPException(status_code=400, detail="结束时间必须晚于开始时间")
    return start, end


def _overlaps(first_start, first_end, second_start, second_end) -> bool:
    return first_start < to_naive_utc(second_end) and to_naive_utc(second_start) < first_end


def _minutes_label(minutes: int) -> str:
    hours, remainder = divmod(max(0, minutes), 60)
    if not hours:
        return f"{remainder} 分钟"
    if not remainder:
        return f"{hours} 小时"
    return f"{hours} 小时 {remainder} 分钟"


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


def _planning_scope(context: PlanningContext) -> IntervalTuple:
    return (
        min(start for start, _ in context.windows),
        max(end for _, end in context.windows),
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
        timezone_name=prefs.timezone,
        requested_mode=payload.planning_mode,
    )


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
