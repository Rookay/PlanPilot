# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：schedule_tools.py
"""Schedule 相关工具。"""

from typing import Any, List

from sqlalchemy.orm import Session

from app.models.schedule import Schedule
from app.schemas.schedule import ScheduleUpdate
from app.services import schedule_service
from app.tools.base import Tool, ToolResult
from app.tools.serializers import schedule_to_dict
from app.utils import parse_naive_utc


class GetSchedulesTool(Tool):
    name = "get_schedules"
    description = "查询日程列表（规划结果）。"
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    output_schema = {"type": "array", "items": {"type": "object"}}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        schedules = schedule_service.list_schedules(db)
        return ToolResult(
            success=True,
            data=[schedule_to_dict(s) for s in schedules],
            summary=f"共 {len(schedules)} 条日程",
        )


class FindFreeSlotsTool(Tool):
    name = "find_free_slots"
    description = "查找某时间范围内的空闲时间段。"
    input_schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "起始时间 ISO 8601"},
            "days": {"type": "integer", "description": "查找天数，默认 1"},
            "min_duration_minutes": {
                "type": "integer",
                "description": "最短时长（分钟），默认 30",
            },
        },
        "required": ["start"],
        "additionalProperties": False,
    }
    output_schema = {"type": "array", "items": {"type": "object"}}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        start = parse_naive_utc(kwargs.get("start"))
        if start is None:
            return ToolResult(success=False, error="无法解析 start 时间")

        days = int(kwargs.get("days", 1))
        min_duration = int(kwargs.get("min_duration_minutes", 30))

        slots: List = schedule_service.find_free_slots(db, start, days, min_duration)
        data = [
            {"start": s.isoformat() + "Z", "end": e.isoformat() + "Z"}
            for s, e in slots
        ]
        return ToolResult(
            success=True,
            data=data,
            summary=f"共找到 {len(data)} 个空闲时间段",
        )


class CreateScheduleTool(Tool):
    name = "create_schedule"
    description = "为某个任务手动创建一条日程。"
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer"},
            "start_time": {"type": "string", "description": "ISO 8601"},
            "end_time": {"type": "string", "description": "ISO 8601"},
        },
        "required": ["task_id", "start_time", "end_time"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "auto"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        start = parse_naive_utc(kwargs.get("start_time"))
        end = parse_naive_utc(kwargs.get("end_time"))
        if start is None or end is None:
            return ToolResult(success=False, error="时间格式非法")

        try:
            schedule = schedule_service.create_schedule(
                db, kwargs["task_id"], start, end
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data=schedule_to_dict(schedule),
            summary=f"已创建日程 #{schedule.id}",
        )


class UpdateScheduleTool(Tool):
    name = "update_schedule"
    description = "修改日程时间或状态。"
    input_schema = {
        "type": "object",
        "properties": {
            "schedule_id": {"type": "integer"},
            "start_time": {"type": "string"},
            "end_time": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["proposed", "accepted", "completed", "cancelled"],
            },
            "is_locked": {"type": "boolean", "description": "锁定后自动规划不会移动该日程"},
        },
        "required": ["schedule_id"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "confirm"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        schedule_id = kwargs.pop("schedule_id")

        update = dict(kwargs)
        for key in ("start_time", "end_time"):
            if key in update and update[key] is not None:
                update[key] = parse_naive_utc(update[key])

        try:
            schedule = schedule_service.update_schedule(
                db, schedule_id, ScheduleUpdate(**update)
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data=schedule_to_dict(schedule),
            summary=f"已更新日程 #{schedule.id}",
        )

    def preview(self, db: Session, **kwargs: Any) -> ToolResult:
        schedule_id = kwargs.get("schedule_id")
        schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
        if schedule is None:
            return ToolResult(success=False, error="Schedule not found")
        update = dict(kwargs)
        update.pop("schedule_id", None)
        for key in ("start_time", "end_time"):
            if key in update:
                update[key] = parse_naive_utc(update[key])
        if set(update) == {"is_locked"}:
            preview_data = {"valid": True, "warnings": [], "conflicts": []}
        else:
            try:
                preview = schedule_service.preview_schedule_update(
                    db, schedule_id, ScheduleUpdate(**update)
                )
            except Exception as exc:  # noqa: BLE001
                return ToolResult(success=False, error=str(exc))
            if not preview.valid:
                return ToolResult(success=False, error="；".join(preview.conflicts))
            preview_data = preview.model_dump()
        action = "锁定状态" if set(update) == {"is_locked"} else "时间或状态"
        return ToolResult(
            success=True,
            needs_confirmation=True,
            data=preview_data,
            summary=f"将修改日程 #{schedule.id} 的{action}",
        )


class DeleteScheduleTool(Tool):
    name = "delete_schedule"
    description = "删除单条日程。"
    input_schema = {
        "type": "object",
        "properties": {"schedule_id": {"type": "integer"}},
        "required": ["schedule_id"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "confirm"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            result = schedule_service.delete_schedule(db, kwargs["schedule_id"])
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True, data=result, summary=f"已删除日程 #{result['id']}"
        )

    def preview(self, db: Session, **kwargs: Any) -> ToolResult:
        schedule = db.query(Schedule).filter(Schedule.id == kwargs["schedule_id"]).first()
        if schedule is None:
            return ToolResult(success=False, error="Schedule not found")
        return ToolResult(
            success=True,
            needs_confirmation=True,
            summary=(
                f"将删除日程 #{schedule.id}（任务 #{schedule.task_id}，"
                f"{schedule.start_time} - {schedule.end_time}）"
            ),
        )
