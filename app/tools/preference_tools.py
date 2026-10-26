# -*- coding: UTF-8 -*-
"""读取和更新长期规划偏好的 Agent 工具。"""

from typing import Any

from sqlalchemy.orm import Session

from app.schemas.preference import PreferenceUpdate
from app.services import preference_service
from app.tools.base import Tool, ToolResult


class GetPreferencesTool(Tool):
    name = "get_preferences"
    description = "读取用户的时区、工作时间、午休、默认间隔和规划策略。"
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        settings = preference_service.get_preferences(db)
        return ToolResult(
            success=True,
            data=settings.model_dump(),
            summary="已读取规划偏好",
        )


class UpdatePreferencesTool(Tool):
    name = "update_preferences"
    description = "更新长期规划偏好；只传需要修改的字段。"
    input_schema = {
        "type": "object",
        "properties": {
            "timezone": {"type": "string", "description": "IANA 时区，如 Asia/Shanghai"},
            "default_location": {"type": "string", "description": "默认活动地点名称"},
            "workday_start": {"type": "string", "description": "HH:MM"},
            "workday_end": {"type": "string", "description": "HH:MM"},
            "lunch_start": {"type": "string", "description": "HH:MM"},
            "lunch_end": {"type": "string", "description": "HH:MM"},
            "default_task_duration_min": {
                "type": "integer",
                "description": "新建任务时的默认时长（分钟）",
            },
            "default_gap_minutes": {
                "type": "integer",
                "description": "相邻日程之间的默认空闲间隔（分钟）",
            },
            "planning_strategy": {
                "type": "string",
                "enum": ["balanced", "deadline_first", "priority_first"],
                "description": "默认排序策略：均衡 / 截止优先 / 优先级优先",
            },
            "schedule_weekends": {
                "type": "boolean",
                "description": "是否把周末也纳入可安排时间",
            },
        },
        "required": [],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "confirm"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            settings = preference_service.update_preferences(
                db, PreferenceUpdate(**kwargs)
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data=settings.model_dump(),
            summary="规划偏好已更新",
        )

    def preview(self, db: Session, **kwargs: Any) -> ToolResult:
        changes = "、".join(f"{key}={value}" for key, value in kwargs.items())
        return ToolResult(
            success=True,
            needs_confirmation=True,
            data={"changes": kwargs},
            summary=f"将更新长期规划偏好：{changes or '无变更'}",
        )
