# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：planning_tools.py
"""规划相关工具：generate_plan / check_conflicts / evaluate_plan / replan。"""

from typing import Any

from sqlalchemy.orm import Session

from app.schemas.schedule import LocalReplanRequest, ScheduleGenerateRequest, ScheduleWindow
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
        planning_mode=kwargs.get("planning_mode", "auto"),
    )


class GeneratePlanTool(Tool):
    name = "generate_plan"
    description = (
        "调用规划引擎，为待办任务生成日程安排（结果以 proposed 状态保存）。"
        "在用户要求「安排/规划」任务时使用。"
        "不得用于旅行、游玩、酒店、景点或接站行程；这些请求使用 create_events。"
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
                        "start": {"type": "string", "description": "窗口开始 ISO 8601 UTC"},
                        "end": {"type": "string", "description": "窗口结束 ISO 8601 UTC"},
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
            "planning_mode": {
                "type": "string",
                "enum": ["auto", "quick", "optimized"],
                "description": "规划方式：自动选择、快速安排或深度优化",
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


class AnalyzePlanRisksTool(Tool):
    name = "analyze_plan_risks"
    description = "分析指定时间范围内的截止、冲突和负载风险，不修改任何数据。"
    input_schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "范围开始时间 ISO 8601 UTC"},
            "end": {"type": "string", "description": "范围结束时间 ISO 8601 UTC"},
        },
        "required": ["start", "end"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        start = parse_naive_utc(kwargs.get("start"))
        end = parse_naive_utc(kwargs.get("end"))
        if start is None or end is None:
            return ToolResult(success=False, error="必须提供有效的风险分析时间范围")
        try:
            report = schedule_service.analyze_plan_risks(db, start, end)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data=report.model_dump(mode="json"),
            summary=(
                f"计划健康度 {report.health_score} 分，"
                f"发现 {len(report.risks)} 项风险"
            ),
        )


class EvaluatePlanTool(Tool):
    name = "evaluate_plan"
    description = "干跑规划并评估质量（分数 + 问题列表），不修改数据。"
    input_schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "评估起点 ISO 8601 UTC，默认当前时间"},
            "days": {"type": "integer", "description": "评估天数，默认 7"},
            "windows": {
                "type": "array",
                "description": "只在指定时间窗口内试排，如今天晚上、明天下午",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string", "description": "窗口开始 ISO 8601 UTC"},
                        "end": {"type": "string", "description": "窗口结束 ISO 8601 UTC"},
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
                "description": "排序策略：均衡 / 截止优先 / 优先级优先",
            },
            "planning_mode": {
                "type": "string",
                "enum": ["auto", "quick", "optimized"],
                "description": "规划方式：自动选择、快速安排或深度优化",
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


class ComparePlanAlternativesTool(Tool):
    name = "compare_plan_alternatives"
    description = "只读生成截止优先、均衡节奏和集中处理三套规划方案供用户比较。"
    input_schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "方案起点 ISO 8601 UTC，默认当前时间"},
            "days": {"type": "integer", "description": "规划天数，默认 7"},
            "windows": {
                "type": "array",
                "description": "只在指定时间窗口内排，如今天晚上、明天下午",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string", "description": "窗口开始 ISO 8601 UTC"},
                        "end": {"type": "string", "description": "窗口结束 ISO 8601 UTC"},
                    },
                    "required": ["start", "end"],
                },
            },
            "gap_minutes": {
                "type": "integer",
                "description": "相邻任务之间的最小空闲间隔（分钟），默认 0",
            },
        },
        "required": [],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            result = schedule_service.generate_plan_alternatives(
                db, _build_generate_payload(kwargs)
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        recommended = next(
            item for item in result.alternatives if item.recommended
        )
        return ToolResult(
            success=True,
            data=result.model_dump(mode="json"),
            summary=(
                f"已生成 3 套方案，推荐「{recommended.title}」："
                f"{recommended.recommendation_reason}"
            ),
        )


class ApplyPlanAlternativeTool(Tool):
    name = "apply_plan_alternative"
    description = "在用户选定后应用一套规划方案，替换当前范围内未锁定的待确认安排。"
    input_schema = {
        "type": "object",
        "properties": {
            **ComparePlanAlternativesTool.input_schema["properties"],
            "profile": {
                "type": "string",
                "enum": ["deadline", "balanced", "focus"],
                "description": "要应用的方案：截止优先 deadline / 均衡 balanced / 集中处理 focus，"
                "取值来自 compare_plan_alternatives 的返回",
            },
        },
        "required": ["profile"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "confirm"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            result = schedule_service.apply_plan_alternative(
                db,
                _build_generate_payload(kwargs),
                kwargs["profile"],
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data={
                "profile": kwargs["profile"],
                "schedules": [
                    {
                        "id": item.id,
                        "task_id": item.task_id,
                        "start_time": item.start_time.isoformat() + "Z",
                        "end_time": item.end_time.isoformat() + "Z",
                    }
                    for item in result.schedules
                ],
                "analysis": result.analysis.model_dump(),
                "batch_id": result.batch_id,
            },
            summary=f"已应用方案，生成 {len(result.schedules)} 条待确认安排",
        )

    def preview(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            result = schedule_service.generate_plan_alternatives(
                db, _build_generate_payload(kwargs)
            )
            alternative = next(
                item for item in result.alternatives
                if item.profile == kwargs["profile"]
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        counts = preview.get("comparison", {}).get("counts", {})
        unscheduled_count = len(preview.get("unscheduled", []))
        return ToolResult(
            success=True,
            needs_confirmation=True,
            data=alternative.model_dump(mode="json"),
            summary=(
                f"将应用「{alternative.title}」，生成 {len(alternative.schedules)} 个时间块，"
                f"替换当前范围内未锁定的待确认安排"
            ),
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
            "start": {"type": "string", "description": "重新规划起点 ISO 8601 UTC，默认当前时间"},
            "days": {"type": "integer", "description": "重排天数，默认 7"},
            "windows": {
                "type": "array",
                "description": "只在指定时间窗口内重排，如今天晚上、明天下午",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string", "description": "窗口开始 ISO 8601 UTC"},
                        "end": {"type": "string", "description": "窗口结束 ISO 8601 UTC"},
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
                "description": "排序策略：均衡 / 截止优先 / 优先级优先",
            },
            "planning_mode": {
                "type": "string",
                "enum": ["auto", "quick", "optimized"],
                "description": "规划方式：自动选择、快速安排或深度优化",
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


class LocalReplanTool(Tool):
    name = "replan_local"
    description = "在指定时间范围内保留锁定日程，只重排未锁定任务安排。"
    input_schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "范围开始时间 ISO 8601 UTC"},
            "end": {"type": "string", "description": "范围结束时间 ISO 8601 UTC"},
            "gap_minutes": {
                "type": "integer",
                "description": "相邻任务之间的最小空闲间隔（分钟），默认 0",
            },
            "strategy": {
                "type": "string",
                "enum": ["balanced", "deadline_first", "priority_first"],
                "description": "排序策略：均衡 / 截止优先 / 优先级优先",
            },
            "planning_mode": {
                "type": "string",
                "enum": ["auto", "quick", "optimized"],
                "description": "规划方式：自动选择、快速安排或深度优化",
            },
            "include_unscheduled": {
                "type": "boolean",
                "description": "是否把此前没排上的任务也纳入本次重排，默认 true",
            },
        },
        "required": ["start", "end"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "confirm"

    @staticmethod
    def _payload(kwargs: dict) -> LocalReplanRequest:
        return LocalReplanRequest(
            start=parse_naive_utc(kwargs.get("start")),
            end=parse_naive_utc(kwargs.get("end")),
            gap_minutes=kwargs.get("gap_minutes"),
            strategy=kwargs.get("strategy"),
            planning_mode=kwargs.get("planning_mode", "auto"),
            include_unscheduled=bool(kwargs.get("include_unscheduled", False)),
        )

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            result = schedule_service.apply_local_replan(db, self._payload(kwargs))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data=result,
            summary=f"局部重排完成，共生成 {len(result['schedules'])} 条安排",
        )

    def preview(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            preview = schedule_service.local_replan_preview(db, self._payload(kwargs))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            needs_confirmation=True,
            data=preview,
            summary=(
                f"将保留 {preview['kept_locked']} 条锁定日程，"
                f"移动 {counts.get('moved', 0)} 条、新增 {counts.get('added', 0)} 条、"
                f"移除 {counts.get('removed', 0)} 条；"
                f"{unscheduled_count} 项暂时无法安排"
            ),
        )


class UndoPlanTool(Tool):
    name = "undo_latest_plan"
    description = "撤销最近一次日程规划或调整，恢复修改前的日程。"
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "confirm"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            result = schedule_service.undo_latest_plan(db)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data=result,
            summary=f"已撤销计划版本 {result['version']['id']}",
        )

    def preview(self, db: Session, **kwargs: Any) -> ToolResult:
        versions = schedule_service.list_plan_versions(db, 100)
        active = next((item for item in versions if item["status"] == "active"), None)
        if active is None:
            return ToolResult(success=False, error="没有可撤销的计划修改")
        return ToolResult(
            success=True,
            needs_confirmation=True,
            data=active,
            summary=f"将撤销：{active['summary']}",
        )
