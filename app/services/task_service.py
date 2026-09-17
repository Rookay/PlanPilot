# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：task_service.py
"""任务业务逻辑，由 API 路由和 Agent Tool 共用。"""

from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.schedule import Schedule
from app.models.task import Task
from app.schemas.task import TaskCreate, TaskUpdate
from app.services import location_service
from app.utils import to_naive_utc


_TIME_FIELDS = ("start_time", "deadline", "earliest_start")


def _normalize_task_times(data: dict) -> dict:
    for field in _TIME_FIELDS:
        if field in data:
            data[field] = to_naive_utc(data[field])
    return data


def create_task(db: Session, data: TaskCreate) -> Task:
    values = _normalize_task_times(data.model_dump())
    _validate_location(db, values.get("location_id"))
    _validate_coordinates(values.get("location_longitude"), values.get("location_latitude"))
    task = Task(**values)
    db.add(task)
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
    if "location_id" in update_data:
        _validate_location(db, update_data["location_id"])
    for key, value in update_data.items():
        setattr(task, key, value)
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

    db.delete(task)
    db.commit()

    return {"message": "Task deleted", "id": task_id}


def _validate_location(db: Session, location_id: int | None) -> None:
    if location_id is not None:
        location_service.get_location(db, location_id)


def _validate_coordinates(longitude: float | None, latitude: float | None) -> None:
    if (longitude is None) != (latitude is None):
        raise HTTPException(status_code=400, detail="地点经度和纬度必须同时提供")
