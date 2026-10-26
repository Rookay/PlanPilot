# -*- coding: UTF-8 -*-
"""多方案对比：生成备选、量化差异、落地所选方案。

"先保临期 / 均衡推进 / 集中完成"的差别只在于任务选择规则与安排节奏，
所以对比逻辑集中在这里，避免和单方案生成互相污染。
"""

from datetime import timezone
from typing import Dict, List
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.schedule import Schedule
from app.planning.engine import (
    PlanAssignment,
    PlanResult,
)
from app.schemas.schedule import (
    PlanAlternative,
    PlanAlternativeMetrics,
    PlanAlternativesResponse,
    ScheduleGenerateRequest,
    ScheduleGenerateResponse,
)
from app.services import preference_service

from app.services.schedule.comparison import (
    _assignment_dict,
    _build_plan_comparison,
    _schedule_dict,
)
from app.services.schedule.core import (
    IntervalTuple,
    PlanningContext,
    _analysis,
    _build_context,
    _minutes_label,
    _planning_scope,
    _run_plan,
    _unscheduled,
)
from app.services.schedule.plans import generate_schedules


def _alternative_payload(
    db: Session,
    payload: ScheduleGenerateRequest,
    profile: str,
) -> ScheduleGenerateRequest:
    prefs = preference_service.get_preferences(db)
    base_gap = (
        payload.gap_minutes
        if payload.gap_minutes is not None
        else prefs.default_gap_minutes
    )
    updates = {
        "deadline": {"strategy": "deadline_first", "gap_minutes": base_gap},
        "balanced": {"strategy": "balanced", "gap_minutes": max(base_gap, 30)},
        "focus": {"strategy": "priority_first", "gap_minutes": 0},
    }
    if profile not in updates:
        raise HTTPException(status_code=400, detail="未知的规划方案")
    return payload.model_copy(
        update={
            **updates[profile],
            "planning_mode": "optimized",
        }
    )


def _alternative_metrics(
    result: PlanResult,
    context: PlanningContext,
    comparison: dict,
) -> PlanAlternativeMetrics:
    zone = ZoneInfo(context.timezone_name)
    assignments = sorted(result.assignments, key=lambda item: (item.start_time, item.end_time))
    daily: Dict[object, List[PlanAssignment]] = {}
    for assignment in assignments:
        day = assignment.start_time.replace(tzinfo=timezone.utc).astimezone(zone).date()
        daily.setdefault(day, []).append(assignment)
    daily_minutes = [
        sum(int((item.end_time - item.start_time).total_seconds() // 60) for item in items)
        for items in daily.values()
    ]
    idle_minutes = 0
    for items in daily.values():
        ordered = sorted(items, key=lambda item: item.start_time)
        idle_minutes += sum(
            max(0, int((following.start_time - previous.end_time).total_seconds() // 60))
            for previous, following in zip(ordered, ordered[1:])
        )
    counts = comparison.get("counts", {})
    return PlanAlternativeMetrics(
        scheduled_tasks=len({item.task_id for item in assignments}),
        unscheduled_tasks=len({item.task_id for item in result.failures}),
        scheduled_minutes=sum(
            int((item.end_time - item.start_time).total_seconds() // 60)
            for item in assignments
        ),
        days_used=len(daily),
        max_daily_minutes=max(daily_minutes, default=0),
        idle_minutes=idle_minutes,
        changed_blocks=(
            counts.get("added", 0)
            + counts.get("moved", 0)
            + counts.get("removed", 0)
        ),
    )


def _alternative_explanations(prepared: list[dict], recommended: str) -> dict:
    """Turn optimizer metrics into concise, deterministic decision explanations."""

    best_score = max(item["analysis"].score for item in prepared)
    min_unscheduled = min(item["metrics"].unscheduled_tasks for item in prepared)
    min_daily = min(item["metrics"].max_daily_minutes for item in prepared)
    min_idle = min(item["metrics"].idle_minutes for item in prepared)
    min_changes = min(item["metrics"].changed_blocks for item in prepared)
    max_scheduled = max(item["metrics"].scheduled_tasks for item in prepared)
    total_tasks = max(
        item["metrics"].scheduled_tasks + item["metrics"].unscheduled_tasks
        for item in prepared
    )
    output = {}
    for item in prepared:
        metrics = item["metrics"]
        strengths = []
        tradeoffs = []
        outcome_labels = []
        if metrics.scheduled_tasks == max_scheduled:
            outcome_labels.append("已安排最多")
        if metrics.max_daily_minutes == min_daily:
            outcome_labels.append("单日负荷最低")
        if metrics.idle_minutes == min_idle:
            outcome_labels.append("任务最紧凑")
        if metrics.changed_blocks == min_changes:
            outcome_labels.append("改动最少")
        if metrics.unscheduled_tasks == min_unscheduled:
            strengths.append(f"可安排 {metrics.scheduled_tasks}/{total_tasks} 项任务")
        if item["analysis"].score == best_score:
            strengths.append(f"计划评分最高（{best_score} 分）")
        if metrics.max_daily_minutes == min_daily:
            strengths.append(f"单日最高负荷最低（{_minutes_label(min_daily)}）")
        if metrics.idle_minutes == min_idle:
            strengths.append(f"任务间空档最少（{_minutes_label(min_idle)}）")
        if metrics.changed_blocks == min_changes:
            strengths.append(f"对现有安排改动最少（{min_changes} 个时间块）")

        if metrics.unscheduled_tasks:
            tradeoffs.append(f"仍有 {metrics.unscheduled_tasks} 项任务无法安排")
        if metrics.max_daily_minutes > min_daily:
            tradeoffs.append(
                f"单日最高负荷比最轻方案多 {_minutes_label(metrics.max_daily_minutes - min_daily)}"
            )
        if metrics.idle_minutes > min_idle:
            tradeoffs.append(
                f"任务间空档比最紧凑方案多 {_minutes_label(metrics.idle_minutes - min_idle)}"
            )
        if metrics.changed_blocks > min_changes:
            tradeoffs.append(f"需要多调整 {metrics.changed_blocks - min_changes} 个时间块")
        if not strengths:
            strengths.append(item["summary"])
        if not tradeoffs:
            tradeoffs.append("没有明显额外代价")

        reason = None
        if item["profile"] == recommended:
            reason = "综合完成数量、计划评分、单日负荷和调整范围，" + "；".join(
                strengths[:2]
            )
        output[item["profile"]] = {
            "strengths": strengths[:3],
            "tradeoffs": tradeoffs[:2],
            "outcome_labels": outcome_labels[:3],
            "recommendation_reason": reason,
        }
    return output


def generate_plan_alternatives(
    db: Session,
    payload: ScheduleGenerateRequest,
) -> PlanAlternativesResponse:
    """Generate three comparable plans without changing persisted schedules."""

    profiles = (
        (
            "deadline",
            "先保临期",
            "先保住真正临近的截止任务；截止较远时仍会综合任务优先级。",
            "任务选择：截止越近，保护力度越高",
            "节奏：可能更轻松，也可能更集中，取决于临期任务量",
        ),
        (
            "balanced",
            "均衡推进",
            "兼顾任务优先级与休息间隔，避免把任务过度堆在一起。",
            "任务选择：优先级为主，同级任务照顾截止时间",
            "节奏：至少保留 30 分钟间隔",
        ),
        (
            "focus",
            "集中完成",
            "优先处理高优先级任务，并尽量压缩任务之间的碎片时间。",
            "任务选择：高优先级任务获得更强保护",
            "节奏：连续处理任务，换取更完整的空闲时段",
        ),
    )
    prepared = []
    shared_checker = None
    current = None
    preserved_count = 0
    scope = None
    for profile, title, summary, selection_policy, pace in profiles:
        profile_payload = _alternative_payload(db, payload, profile)
        context = _build_context(db, profile_payload)
        if shared_checker is None:
            shared_checker = context.travel_checker
        else:
            context.travel_checker = shared_checker
        result = _run_plan(context)
        scope_start, scope_end = _planning_scope(context)
        if current is None:
            scope = (scope_start, scope_end)
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
        comparison = _build_plan_comparison(
            current or [],
            result.assignments,
            preserved_count=preserved_count,
        )
        analysis = _analysis(result, context)
        metrics = _alternative_metrics(result, context, comparison)
        rank = (
            analysis.score * 10
            + metrics.scheduled_tasks * 100
            - metrics.unscheduled_tasks * 200
            - max(0, metrics.max_daily_minutes - 8 * 60) // 15
            - metrics.idle_minutes // 60
            - metrics.changed_blocks
        )
        prepared.append(
            {
                "profile": profile,
                "title": title,
                "summary": summary,
                "selection_policy": selection_policy,
                "pace": pace,
                "result": result,
                "analysis": analysis,
                "metrics": metrics,
                "comparison": comparison,
                "rank": rank,
            }
        )

    recommended = max(
        prepared,
        key=lambda item: (
            item["rank"],
            item["profile"] == "balanced",
            item["profile"] == "deadline",
        ),
    )["profile"]
    explanations = _alternative_explanations(prepared, recommended)
    alternatives = [
        PlanAlternative(
            profile=item["profile"],
            title=item["title"],
            summary=item["summary"],
            selection_policy=item["selection_policy"],
            pace=item["pace"],
            outcome_labels=explanations[item["profile"]]["outcome_labels"],
            recommended=item["profile"] == recommended,
            strengths=explanations[item["profile"]]["strengths"],
            tradeoffs=explanations[item["profile"]]["tradeoffs"],
            recommendation_reason=explanations[item["profile"]]["recommendation_reason"],
            schedules=[_assignment_dict(value) for value in item["result"].assignments],
            unscheduled=_unscheduled(item["result"]),
            analysis=item["analysis"],
            metrics=item["metrics"],
            comparison=item["comparison"],
        )
        for item in prepared
    ]
    return PlanAlternativesResponse(
        alternatives=alternatives,
        recommended_profile=recommended,
        scope={"start": scope[0], "end": scope[1]},
    )


def apply_plan_alternative(
    db: Session,
    payload: ScheduleGenerateRequest,
    profile: str,
) -> ScheduleGenerateResponse:
    return generate_schedules(db, _alternative_payload(db, payload, profile))
