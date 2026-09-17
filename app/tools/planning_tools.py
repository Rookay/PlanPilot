# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：planning_tools.py
"""规划相关工具：generate_plan / check_conflicts / evaluate_plan / replan。"""

from typing import Any

from sqlalchemy.orm import Session

from app.schemas.schedule import ScheduleGenerateRequest, ScheduleWindow
from app.services import schedule_service
from app.tools.base import Tool, ToolResult
from app.utils import parse_naive_utc


def _build_generate_payload(kwargs: dict) -> ScheduleGenerateRequest:
    windows = None
    raw_windows = kwargs.get("windows")
    if raw_windows:
        parsed = []
        for window in raw_windows:
            if not isinstance(window, dict):
                continue
            start = parse_naive_utc(window.get("start"))
            end = parse_naive_utc(window.get("end"))
            if start is not None and end is not None:
                parsed.append(ScheduleWindow(start=start, end=end))
        if parsed:
            windows = parsed

    gap = kwargs.get("gap_minutes")
    return ScheduleGenerateRequest(
        start=parse_naive_utc(kwargs.get("start")),
        days=int(kwargs.get("days", 7)),
        windows=windows,
        gap_minutes=int(gap) if gap is not None else None,
        strategy=kwargs.get("strategy"),
    )


class GeneratePlanTool(Tool):
    name = "generate_plan"
    description = (
        "调用规划引擎，为待办任务生成日程安排（结果以 proposed 状态保存）。"
        "在用户要求「安排/规划」任务时使用。"
        "用户指定了时间段（如「今天晚上」「明天下午」）时，用 windows 参数传入这些时间窗口；"
        "用户要求「留点空闲/不要连续」时，用 gap_minutes 传入间隔分钟数。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "起始时间 ISO 8601（UTC），可选"},
            "days": {"type": "integer", "description": "规划天数，默认 7"},
            "windows": {
                "type": "array",
                "description": "允许安排的时间窗口列表（如今天晚上、明天下午），每个元素含 start/end（ISO 8601 UTC）",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                    },
                    "required": ["start", "end"],
                },
            },
            "gap_minutes": {
                "type": "integer",
                "description": "相邻任务之间的最小空闲间隔（分钟），默认 0",
            },
            "strategy": {
                "type": "string",
                "enum": ["balanced", "deadline_first", "priority_first"],
                "description": "规划排序策略",
            },
        },
        "required": [],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "auto"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            result = schedule_service.generate_schedules(db, _build_generate_payload(kwargs))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))

        return ToolResult(
            success=True,
            data={
                "schedules": [
                    {
                        "id": s.id,
                        "task_id": s.task_id,
                        "start_time": s.start_time.isoformat() + "Z",
                        "end_time": s.end_time.isoformat() + "Z",
                        "status": s.status,
                        "reason": s.reason,
                        "plan_batch_id": s.plan_batch_id,
                    }
                    for s in result.schedules
                ],
                "unscheduled": [
                    {"task_id": u.id, "title": u.title, "reason": u.reason}
                    for u in result.unscheduled
                ],
                "analysis": result.analysis.model_dump(),
                "batch_id": result.batch_id,
            },
            summary=f"已生成 {len(result.schedules)} 条日程，"
            f"{len(result.unscheduled)} 个任务未能安排",
        )


class CheckConflictsTool(Tool):
    name = "check_conflicts"
    description = "检测当前事件与日程之间的时间冲突。"
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        conflicts = schedule_service.check_conflicts(db)
        data = [
            {
                "first": {
                    "id": c.first.id,
                    "title": c.first.title,
                    "start": c.first.start.isoformat() + "Z",
                    "end": c.first.end.isoformat() + "Z",
                },
                "second": {
                    "id": c.second.id,
                    "title": c.second.title,
                    "start": c.second.start.isoformat() + "Z",
                    "end": c.second.end.isoformat() + "Z",
                },
                "overlap_minutes": c.overlap_minutes,
            }
            for c in conflicts
        ]
        return ToolResult(
            success=True,
            data=data,
            summary=f"发现 {len(data)} 处冲突" if data else "没有冲突",
        )


class EvaluatePlanTool(Tool):
    name = "evaluate_plan"
    description = "干跑规划并评估质量（分数 + 问题列表），不修改数据。"
    input_schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string"},
            "days": {"type": "integer"},
            "windows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                    },
                    "required": ["start", "end"],
                },
            },
            "gap_minutes": {"type": "integer"},
            "strategy": {
                "type": "string",
                "enum": ["balanced", "deadline_first", "priority_first"],
            },
        },
        "required": [],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            evaluation = schedule_service.evaluate_plan_dry_run(
                db, _build_generate_payload(kwargs)
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))

        return ToolResult(
            success=True,
            data={
                "score": evaluation.score,
                "issues": evaluation.issues,
                "scheduled_count": evaluation.scheduled_count,
                "unscheduled_count": evaluation.unscheduled_count,
                "scheduled_minutes": evaluation.scheduled_minutes,
            },
            summary=f"计划评分 {evaluation.score}/100",
        )


class ReplanTool(Tool):
    name = "replan"
    description = (
        "重新规划：保留已确认日程，重新生成未确认日程。"
        "在用户增加/修改/取消事件或任务后需要调整安排时使用。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string"},
            "days": {"type": "integer"},
            "windows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                    },
                    "required": ["start", "end"],
                },
            },
            "gap_minutes": {"type": "integer"},
            "strategy": {
                "type": "string",
                "enum": ["balanced", "deadline_first", "priority_first"],
            },
        },
        "required": [],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "confirm"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        return GeneratePlanTool().execute(db, **kwargs)

    def preview(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            preview = schedule_service.replan_preview(
                db, _build_generate_payload(kwargs)
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))

        removed_count = len(preview["removed"])
        proposed_count = len(preview["proposed"])
        return ToolResult(
            success=True,
            needs_confirmation=True,
            data=preview,
            summary=f"将移除 {removed_count} 条旧日程，生成 {proposed_count} 条新日程",
        )
