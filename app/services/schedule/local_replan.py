# -*- coding: UTF-8 -*-
"""局部重排：锁定不该动的日程，只重排受影响的范围。

与整份重规划的区别在"保留哪一部分"：先用锁定集合算出影响范围，再只在
这个范围内跑引擎，最后给出移动 / 新增 / 移除的逐项对比。
"""

import uuid
from typing import Dict, List

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.schedule import Schedule
from app.models.task import Task
from app.planning.engine import (
    PlanAssignment,
    PlanResult,
)
from app.schemas.schedule import (
    LocalReplanRequest,
    ScheduleGenerateRequest,
)
from app.services import plan_version_service, time_policy
from app.utils import to_naive_utc

from app.services.schedule.comparison import (
    _assignment_dict,
    _build_plan_comparison,
)
from app.services.schedule.core import (
    IntervalTuple,
    PlanningContext,
    _analysis,
    _build_context,
    _existing_chunk_offsets,
    _normalize_range,
    _run_plan,
    _schedule_dict,
    _unscheduled,
)


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
