# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：external_tools.py
"""外部/通用工具。第一版先提供 get_current_time。"""

from app.tools.base import Tool, ToolResult
from app.utils import utcnow

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


class GetCurrentTimeTool(Tool):
    name = "get_current_time"
    description = (
        "获取当前日期和时间（UTC）。当用户提到「今天/明天/现在」等相对时间，"
        "或需要以当前时间为基准进行推算时使用。"
    )
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "utc_now": {"type": "string"},
            "date": {"type": "string"},
            "time": {"type": "string"},
            "weekday": {"type": "string"},
            "iso_weekday": {"type": "integer"},
        },
    }
    confirmation = "none"

    def execute(self, **kwargs) -> ToolResult:
        now = utcnow()
        return ToolResult(
            success=True,
            data={
                "utc_now": now.isoformat(),
                "date": now.strftime("%Y-%m-%d"),
                "time": now.strftime("%H:%M:%S"),
                "weekday": _WEEKDAYS[now.weekday()],
                "iso_weekday": now.isoweekday(),
            },
        )
