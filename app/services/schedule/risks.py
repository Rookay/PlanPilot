# -*- coding: UTF-8 -*-
"""范围风险分析：截止、容量、冲突与连续工作。

只读——不写任何表，也不改日程。
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.schedule import Schedule
from app.models.task import Task
from app.planning.engine import (
    Interval,
    check_conflicts as engine_check_conflicts,
    find_free_slots as engine_find_free_slots,
)
from app.planning.windows import (
    build_local_work_windows,
    lunch_intervals,
)
from app.schemas.preference import PreferenceSettings
from app.schemas.schedule import (
    PlanRisk,
    PlanRiskReport,
)
from app.services import preference_service, time_policy
from app.utils import to_naive_utc

from app.services.schedule.core import IntervalTuple, _normalize_range


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
