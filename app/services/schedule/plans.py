# -*- coding: UTF-8 -*-
"""计划生成、预览与试算。

负责"把一次规划请求变成候选方案"：按范围生成、生成前预览、重规划预览
以及不落库的试算。多方案对比见 :mod:`alternatives`。
"""

import uuid
from typing import List

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.schedule import Schedule
from app.planning.engine import (
    Evaluation,
)
from app.schemas.schedule import (
    ScheduleGenerateRequest,
    ScheduleGenerateResponse,
)
from app.services import plan_version_service

from app.services.schedule.comparison import _build_plan_comparison
from app.services.schedule.core import (
    IntervalTuple,
    _analysis,
    _build_context,
    _existing_chunk_offsets,
    _normalize_range,
    _planning_scope,
    _run_plan,
    _schedule_dict,
    _unscheduled,
)


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
