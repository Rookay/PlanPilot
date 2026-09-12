# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：event_tools.py
"""Event 相关工具。"""

from typing import Any

from sqlalchemy.orm import Session

from app.schemas.event import EventCreate, EventUpdate
from app.services import event_service
from app.tools.base import Tool, ToolResult
from app.tools.serializers import event_to_dict
from app.utils import parse_naive_utc


class GetEventsTool(Tool):
    name = "get_events"
    description = "查询事件列表（固定安排，如会议、约会）。"
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    output_schema = {"type": "array", "items": {"type": "object"}}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        events = event_service.list_events(db)
        return ToolResult(
            success=True,
            data=[event_to_dict(e) for e in events],
            summary=f"共 {len(events)} 个事件",
        )


class GetEventTool(Tool):
    name = "get_event"
    description = "按 id 查询单个事件。"
    input_schema = {
        "type": "object",
        "properties": {"event_id": {"type": "integer"}},
        "required": ["event_id"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            event = event_service.get_event(db, kwargs["event_id"])
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(success=True, data=event_to_dict(event))


class CreateEventTool(Tool):
    name = "create_event"
    description = "创建固定事件（不可被规划引擎移动，如会议/约会）。"
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "start_time": {"type": "string", "description": "ISO 8601"},
            "end_time": {"type": "string", "description": "ISO 8601"},
            "description": {"type": "string"},
            "location": {"type": "string"},
        },
        "required": ["title", "start_time", "end_time"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "auto"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        payload = {
            "title": kwargs["title"],
            "start_time": parse_naive_utc(kwargs["start_time"]),
            "end_time": parse_naive_utc(kwargs["end_time"]),
            "description": kwargs.get("description"),
            "location": kwargs.get("location"),
        }
        try:
            event = event_service.create_event(db, EventCreate(**payload))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data=event_to_dict(event),
            summary=f"已创建事件「{event.title}」",
        )


class UpdateEventTool(Tool):
    name = "update_event"
    description = "更新事件字段（时间/地点等）。"
    input_schema = {
        "type": "object",
        "properties": {
            "event_id": {"type": "integer"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "start_time": {"type": "string"},
            "end_time": {"type": "string"},
            "location": {"type": "string"},
        },
        "required": ["event_id"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "auto"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        event_id = kwargs.pop("event_id")

        update = dict(kwargs)
        for key in ("start_time", "end_time"):
            if key in update and update[key] is not None:
                update[key] = parse_naive_utc(update[key])

        try:
            event = event_service.update_event(db, event_id, EventUpdate(**update))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data=event_to_dict(event),
            summary=f"已更新事件「{event.title}」",
        )


class DeleteEventTool(Tool):
    name = "delete_event"
    description = "删除事件。"
    input_schema = {
        "type": "object",
        "properties": {"event_id": {"type": "integer"}},
        "required": ["event_id"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "confirm"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            result = event_service.delete_event(db, kwargs["event_id"])
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True, data=result, summary=f"已删除事件 #{result['id']}"
        )

    def preview(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            event = event_service.get_event(db, kwargs["event_id"])
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            needs_confirmation=True,
            summary=f"将删除事件「{event.title}」",
        )
