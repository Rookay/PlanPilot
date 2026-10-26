# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：task_tools.py
"""Task 相关工具。"""

from typing import Any, List, Optional

from sqlalchemy.orm import Session

from app.models.schedule import Schedule
from app.schemas.task import TaskCreate, TaskUpdate
from app.services import task_service
from app.tools.base import Tool, ToolResult
from app.tools.serializers import task_to_dict
from app.utils import parse_naive_utc


class GetTasksTool(Tool):
    name = "get_tasks"
    description = "查询任务列表。可按状态筛选，或按关键字搜索标题。"
    input_schema = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "cancelled"],
                "description": "可选，按状态筛选",
            },
            "q": {"type": "string", "description": "可选，标题关键字"},
        },
        "required": [],
        "additionalProperties": False,
    }
    output_schema = {"type": "array", "items": {"type": "object"}}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        status: Optional[str] = kwargs.get("status")
        q: Optional[str] = kwargs.get("q")

        tasks = task_service.list_tasks(db, status=status, q=q)

        return ToolResult(
            success=True,
            data=[task_to_dict(t) for t in tasks],
            summary=f"共 {len(tasks)} 个任务",
        )


class GetTaskTool(Tool):
    name = "get_task"
    description = "按 id 查询单个任务详情。"
    input_schema = {
        "type": "object",
        "properties": {"task_id": {"type": "integer", "description": "任务 id，来自 get_tasks 的返回"}},
        "required": ["task_id"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            task = task_service.get_task(db, kwargs["task_id"])
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(success=True, data=task_to_dict(task))


def _build_task_payload(kwargs: dict) -> dict:
    payload = {
        "title": kwargs["title"],
        "description": kwargs.get("description"),
        "estimated_duration": kwargs.get("estimated_duration", 60),
        "splittable": kwargs.get("splittable", False),
        "min_chunk_minutes": kwargs.get("min_chunk_minutes", 30),
        "max_chunk_minutes": kwargs.get("max_chunk_minutes", 120),
        "dependency_ids": kwargs.get("dependency_ids", []),
        "priority": kwargs.get("priority", "medium"),
        "start_time": parse_naive_utc(kwargs.get("start_time")),
        "deadline": parse_naive_utc(kwargs.get("deadline")),
        "earliest_start": parse_naive_utc(kwargs.get("earliest_start")),
        "preferred_time": kwargs.get("preferred_time"),
        "category": kwargs.get("category"),
        "location_id": kwargs.get("location_id"),
        "location": kwargs.get("location"),
        "location_city": kwargs.get("location_city"),
        "location_longitude": kwargs.get("location_longitude"),
        "location_latitude": kwargs.get("location_latitude"),
    }
    return payload


class CreateTaskTool(Tool):
    name = "create_task"
    description = "创建可由规划器安排或移动的任务；没有固定时刻的活动应创建为任务。"
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "任务名称"},
            "description": {"type": "string", "description": "补充说明，可为空"},
            "estimated_duration": {
                "type": "integer",
                "description": "预计时长（分钟），默认 60",
            },
            "priority": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "优先级，默认 medium；用户说“很急/优先”时传 high",
            },
            "splittable": {"type": "boolean", "description": "是否允许拆成多个时间块"},
            "min_chunk_minutes": {"type": "integer", "description": "最小分段分钟数"},
            "max_chunk_minutes": {"type": "integer", "description": "最大分段分钟数"},
            "dependency_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "必须先完成的任务 id",
            },
            "start_time": {"type": "string", "description": "ISO 8601，可选"},
            "deadline": {"type": "string", "description": "ISO 8601，可选"},
            "earliest_start": {"type": "string", "description": "ISO 8601，可选"},
            "preferred_time": {"type": "string", "description": "如 09:00"},
            "category": {"type": "string", "description": "分类标签，如 学习 / 工作 / 生活，可为空"},
            "location_id": {"type": "integer", "description": "常用地点 id，可选"},
            "location": {"type": "string", "description": "临时地点名称或地址"},
            "location_city": {"type": "string", "description": "临时地点所在城市，配合经纬度使用"},
            "location_longitude": {"type": "number", "description": "临时地点经度（WGS84）"},
            "location_latitude": {"type": "number", "description": "临时地点纬度（WGS84）"},
        },
        "required": ["title"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "auto"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            task = task_service.create_task(
                db, TaskCreate(**_build_task_payload(kwargs))
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data=task_to_dict(task),
            summary=f"已创建任务「{task.title}」",
        )


class UpdateTaskTool(Tool):
    name = "update_task"
    description = "更新任务字段（状态/时长/优先级/时间等）。"
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "要更新的任务 id，必填"},
            "title": {"type": "string", "description": "新名称，只在要改名时传"},
            "description": {"type": "string", "description": "新的补充说明"},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "cancelled"],
                "description": "任务状态；标记完成优先用 complete_task",
            },
            "priority": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "优先级；用户说“这个更急”时改成 high",
            },
            "estimated_duration": {"type": "integer", "description": "新的预计时长（分钟）"},
            "splittable": {"type": "boolean", "description": "是否允许拆成多个时间块"},
            "min_chunk_minutes": {"type": "integer", "description": "最小分段分钟数"},
            "max_chunk_minutes": {"type": "integer", "description": "最大分段分钟数"},
            "dependency_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "整体替换的任务依赖 id 列表",
            },
            "start_time": {"type": "string", "description": "ISO 8601 UTC，固定开始时刻"},
            "deadline": {"type": "string", "description": "ISO 8601 UTC，最晚完成时刻"},
            "earliest_start": {"type": "string", "description": "ISO 8601 UTC，最早可开始时刻"},
            "preferred_time": {"type": "string", "description": "偏好时段，如 09:00"},
            "category": {"type": "string", "description": "分类标签"},
            "location_id": {"type": "integer", "description": "常用地点 id，可选"},
            "location": {"type": "string", "description": "临时地点名称或地址"},
            "location_city": {"type": "string", "description": "临时地点所在城市"},
            "location_longitude": {"type": "number", "description": "临时地点经度（WGS84）"},
            "location_latitude": {"type": "number", "description": "临时地点纬度（WGS84）"},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "auto"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        task_id = kwargs.pop("task_id")

        update = dict(kwargs)
        for key in ("start_time", "deadline", "earliest_start"):
            if key in update and update[key] is not None:
                update[key] = parse_naive_utc(update[key])

        try:
            task = task_service.update_task(db, task_id, TaskUpdate(**update))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data=task_to_dict(task),
            summary=f"已更新任务「{task.title}」",
        )


class CompleteTaskTool(Tool):
    name = "complete_task"
    description = "把任务标记为已完成。"
    input_schema = {
        "type": "object",
        "properties": {"task_id": {"type": "integer", "description": "任务 id，来自 get_tasks 的返回"}},
        "required": ["task_id"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "auto"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            task = task_service.update_task(
                db, kwargs["task_id"], TaskUpdate(status="completed")
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data=task_to_dict(task),
            summary=f"任务「{task.title}」已标记为完成",
        )


class DeleteTaskTool(Tool):
    name = "delete_task"
    description = "删除任务（会级联删除其日程）。"
    input_schema = {
        "type": "object",
        "properties": {"task_id": {"type": "integer", "description": "任务 id，来自 get_tasks 的返回"}},
        "required": ["task_id"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "confirm"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            result = task_service.delete_task(db, kwargs["task_id"])
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(success=True, data=result, summary=f"已删除任务 #{result['id']}")

    def preview(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            task = task_service.get_task(db, kwargs["task_id"])
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))

        related = (
            db.query(Schedule).filter(Schedule.task_id == kwargs["task_id"]).count()
        )
        return ToolResult(
            success=True,
            needs_confirmation=True,
            summary=f"将删除任务「{task.title}」及其 {related} 条日程",
        )
