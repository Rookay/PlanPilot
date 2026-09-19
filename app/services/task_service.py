# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：task_service.py
"""任务业务逻辑，由 API 路由和 Agent Tool 共用。"""

from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.schedule import Schedule
from app.models.task import Task, TaskDependency
from app.schemas.task import TaskCreate, TaskUpdate
from app.services import location_service, time_policy
from app.utils import to_naive_utc


_TIME_FIELDS = ("start_time", "deadline", "earliest_start")


def _normalize_task_times(data: dict) -> dict:
    for field in _TIME_FIELDS:
        if field in data:
            data[field] = to_naive_utc(data[field])
    return data


def create_task(db: Session, data: TaskCreate) -> Task:
    values = _normalize_task_times(data.model_dump())
    time_policy.ensure_not_past(values.get("start_time"))
    time_policy.ensure_not_past(values.get("deadline"), "截止时间")
    dependency_ids = values.pop("dependency_ids", [])
    _validate_location(db, values.get("location_id"))
    _validate_coordinates(values.get("location_longitude"), values.get("location_latitude"))
    _validate_chunk_settings(values)
    task = Task(**values)
    db.add(task)
    db.flush()
    _replace_dependencies(db, task.id, dependency_ids)
    db.commit()
    db.refresh(task)
    return task


def list_tasks(
    db: Session,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    category: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = "created_desc",
) -> List[Task]:
    query = db.query(Task)

    if status:
        query = query.filter(Task.status == status)
    if priority:
        query = query.filter(Task.priority == priority)
    if category:
        query = query.filter(Task.category == category)
    if q:
        query = query.filter(Task.title.ilike(f"%{q.strip()}%"))

    order_map = {
        "created_desc": Task.created_at.desc(),
        "deadline_asc": Task.deadline.asc(),
        "duration_asc": Task.estimated_duration.asc(),
        "title_asc": Task.title.asc(),
    }
    return query.order_by(order_map.get(sort, order_map["created_desc"])).all()


def get_task(db: Session, task_id: int) -> Task:
    task = db.query(Task).filter(Task.id == task_id).first()

    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    return task


def update_task(db: Session, task_id: int, data: TaskUpdate) -> Task:
    task = get_task(db, task_id)

    update_data = _normalize_task_times(data.model_dump(exclude_unset=True))
    if "start_time" in update_data:
        time_policy.ensure_not_past(update_data["start_time"])
    if "deadline" in update_data:
        time_policy.ensure_not_past(update_data["deadline"], "截止时间")
    dependency_ids = update_data.pop("dependency_ids", None)
    if "location_id" in update_data:
        _validate_location(db, update_data["location_id"])
    for key, value in update_data.items():
        setattr(task, key, value)
    _validate_chunk_settings(
        {
            "min_chunk_minutes": task.min_chunk_minutes,
            "max_chunk_minutes": task.max_chunk_minutes,
        }
    )
    if dependency_ids is not None:
        _replace_dependencies(db, task.id, dependency_ids)
    _validate_coordinates(task.location_longitude, task.location_latitude)

    if task.deadline and task.earliest_start and task.earliest_start > task.deadline:
        raise HTTPException(status_code=400, detail="最早可开始时间不能晚于截止时间")
    if task.deadline and task.start_time and task.start_time > task.deadline:
        raise HTTPException(status_code=400, detail="开始时间不能晚于截止时间")

    if update_data.get("status") == "completed":
        db.query(Schedule).filter(
            Schedule.task_id == task_id,
            Schedule.status.in_(("proposed", "accepted")),
        ).update({"status": "completed"}, synchronize_session=False)
    elif update_data.get("status") == "cancelled":
        db.query(Schedule).filter(
            Schedule.task_id == task_id,
            Schedule.status.in_(("proposed", "accepted")),
        ).update({"status": "cancelled"}, synchronize_session=False)

    db.commit()
    db.refresh(task)
    return task


def delete_task(db: Session, task_id: int) -> dict:
    task = get_task(db, task_id)

    # 先删除关联日程，避免外键约束报错并防止残留孤立数据。
    db.query(Schedule).filter(Schedule.task_id == task_id).delete(
        synchronize_session=False
    )
    db.query(TaskDependency).filter(
        (TaskDependency.task_id == task_id)
        | (TaskDependency.depends_on_task_id == task_id)
    ).delete(synchronize_session=False)

    db.delete(task)
    db.commit()

    return {"message": "Task deleted", "id": task_id}


def _validate_location(db: Session, location_id: int | None) -> None:
    if location_id is not None:
        location_service.get_location(db, location_id)


def _validate_coordinates(longitude: float | None, latitude: float | None) -> None:
    if (longitude is None) != (latitude is None):
        raise HTTPException(status_code=400, detail="地点经度和纬度必须同时提供")


def _validate_chunk_settings(values: dict) -> None:
    minimum = values.get("min_chunk_minutes", 30)
    maximum = values.get("max_chunk_minutes", 120)
    if minimum > maximum:
        raise HTTPException(status_code=400, detail="最小分段时长不能大于最大分段时长")


def _replace_dependencies(
    db: Session,
    task_id: int,
    dependency_ids: List[int],
) -> None:
    unique_ids = list(dict.fromkeys(dependency_ids))
    if task_id in unique_ids:
        raise HTTPException(status_code=400, detail="任务不能依赖自身")
    existing_ids = {
        row.id for row in db.query(Task).filter(Task.id.in_(unique_ids)).all()
    }
    missing = [item for item in unique_ids if item not in existing_ids]
    if missing:
        raise HTTPException(status_code=404, detail=f"前置任务不存在：{missing}")

    links = db.query(TaskDependency).all()
    graph: dict[int, set[int]] = {}
    for link in links:
        if link.task_id != task_id:
            graph.setdefault(link.task_id, set()).add(link.depends_on_task_id)
    graph[task_id] = set(unique_ids)

    def reaches(start: int, target: int, seen: set[int]) -> bool:
        if start == target:
            return True
        if start in seen:
            return False
        seen.add(start)
        return any(reaches(item, target, seen) for item in graph.get(start, set()))

    if any(reaches(dependency_id, task_id, set()) for dependency_id in unique_ids):
        raise HTTPException(status_code=400, detail="任务依赖不能形成循环")

    db.query(TaskDependency).filter(TaskDependency.task_id == task_id).delete(
        synchronize_session=False
    )
    db.add_all(
        TaskDependency(task_id=task_id, depends_on_task_id=dependency_id)
        for dependency_id in unique_ids
    )
