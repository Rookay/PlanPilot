# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：serializers.py
"""把 ORM 对象转成 JSON 可序列化字典（供 Tool 返回给 LLM / API）。"""

from datetime import datetime


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat() + "Z"  # 项目统一 naive UTC，补 Z 便于 LLM 理解


def task_to_dict(task) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "priority": task.priority,
        "estimated_duration": task.estimated_duration,
        "start_time": _iso(task.start_time),
        "deadline": _iso(task.deadline),
        "earliest_start": _iso(task.earliest_start),
        "preferred_time": task.preferred_time,
        "category": task.category,
    }


def event_to_dict(event) -> dict:
    return {
        "id": event.id,
        "title": event.title,
        "description": event.description,
        "start_time": _iso(event.start_time),
        "end_time": _iso(event.end_time),
        "location": event.location,
    }


def schedule_to_dict(schedule) -> dict:
    return {
        "id": schedule.id,
        "task_id": schedule.task_id,
        "start_time": _iso(schedule.start_time),
        "end_time": _iso(schedule.end_time),
        "status": schedule.status,
    }
