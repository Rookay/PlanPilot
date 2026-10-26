# -*- coding: UTF-8 -*-
"""执行后审计：确认工具真的把该改的改了。

工具调用链很长，中间任何一步静默失败都不会报错。审计在写操作之后重新
读一遍库，逐项核对"声称发生的变更"与"实际落库的状态"是否一致。
"""

from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.location import SavedLocation
from app.models.schedule import Schedule
from app.integrations.maps.base import MapIntegrationError
from app.integrations.maps.service import get_map_service
from app.planning.engine import (
    Interval,
    check_conflicts as engine_check_conflicts,
)
from app.services.travel_planning_service import (
    TravelConstraintChecker,
)
from app.utils import to_naive_utc

from app.services.schedule.core import _fixed_travel_blocks, _task_locations


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
