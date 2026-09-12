# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：external_tools.py
"""外部/通用工具。

第一版：
- get_current_time：真实实现。
- get_weather：占位实现（后续接入真实天气 API）。
"""

from typing import Any

from sqlalchemy.orm import Session

from app.models.preference import UserPreference
from app.tools.base import Tool, ToolResult
from app.utils import local_now, utc_offset_hours, utcnow

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _get_timezone(db: Session) -> str:
    row = db.query(UserPreference).filter(UserPreference.key == "timezone").first()
    return row.value if row else "Asia/Shanghai"


class GetCurrentTimeTool(Tool):
    name = "get_current_time"
    description = (
        "获取当前时间（同时返回 UTC 和用户本地时间）。当用户提到「今天/明天/现在」等相对时间，"
        "或需要以当前时间为基准进行推算时使用。注意：传给其它工具的时间必须是 UTC。"
    )
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        timezone = _get_timezone(db)
        now = utcnow()
        local = local_now(timezone)
        offset = utc_offset_hours(timezone)
        return ToolResult(
            success=True,
            data={
                "utc_now": now.isoformat() + "Z",
                "local_now": local.isoformat(),
                "date": local.strftime("%Y-%m-%d"),
                "time": local.strftime("%H:%M:%S"),
                "weekday": _WEEKDAYS[local.weekday()],
                "iso_weekday": local.isoweekday(),
                "timezone": timezone,
                "utc_offset_hours": offset,
            },
        )


class GetWeatherTool(Tool):
    name = "get_weather"
    description = "查询某日天气（占位实现，尚未接入真实天气数据）。"
    input_schema = {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
            "location": {"type": "string", "description": "城市，可选"},
        },
        "required": ["date"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        # TODO: 接入真实天气 API。
        return ToolResult(
            success=True,
            data={
                "date": kwargs.get("date"),
                "location": kwargs.get("location"),
                "condition": "unknown",
                "note": "天气数据尚未接入",
            },
            summary="天气数据尚未接入",
        )
