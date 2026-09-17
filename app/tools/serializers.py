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
        "splittable": task.splittable,
        "min_chunk_minutes": task.min_chunk_minutes,
        "max_chunk_minutes": task.max_chunk_minutes,
        "dependency_ids": task.dependency_ids,
        "start_time": _iso(task.start_time),
        "deadline": _iso(task.deadline),
        "earliest_start": _iso(task.earliest_start),
        "preferred_time": task.preferred_time,
        "category": task.category,
        "location_id": task.location_id,
        "location": task.location,
        "location_city": task.location_city,
        "location_longitude": task.location_longitude,
        "location_latitude": task.location_latitude,
    }


def event_to_dict(event) -> dict:
    return {
        "id": event.id,
        "title": event.title,
        "description": event.description,
        "start_time": _iso(event.start_time),
        "end_time": _iso(event.end_time),
        "event_type": event.event_type,
        "travel_mode": event.travel_mode,
        "trip_id": event.trip_id,
        "trip_title": event.trip_title,
        "trip_order": event.trip_order,
        "location": event.location,
        "location_id": event.location_id,
        "location_city": event.location_city,
        "location_longitude": event.location_longitude,
        "location_latitude": event.location_latitude,
        "origin_location": event.origin_location,
        "origin_location_id": event.origin_location_id,
        "origin_location_city": event.origin_location_city,
        "origin_location_longitude": event.origin_location_longitude,
        "origin_location_latitude": event.origin_location_latitude,
    }


def schedule_to_dict(schedule) -> dict:
    return {
        "id": schedule.id,
        "task_id": schedule.task_id,
        "start_time": _iso(schedule.start_time),
        "end_time": _iso(schedule.end_time),
        "status": schedule.status,
        "is_locked": schedule.is_locked,
        "chunk_index": schedule.chunk_index,
        "source": schedule.source,
        "plan_version_id": schedule.plan_version_id,
    }
