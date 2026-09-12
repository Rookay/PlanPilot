# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：task_service.py
"""任务业务逻辑。API 路由与未来的 Tool 共用。"""

from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.schedule import Schedule
from app.models.task import Task
from app.schemas.task import TaskCreate, TaskUpdate


def create_task(db: Session, data: TaskCreate) -> Task:
    task = Task(**data.model_dump())
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def list_tasks(db: Session, status: Optional[str] = None) -> List[Task]:
    query = db.query(Task)

    if status:
        query = query.filter(Task.status == status)

    return query.order_by(Task.created_at.desc()).all()


def get_task(db: Session, task_id: int) -> Task:
    task = db.query(Task).filter(Task.id == task_id).first()

    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    return task


def update_task(db: Session, task_id: int, data: TaskUpdate) -> Task:
    task = get_task(db, task_id)

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(task, key, value)

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
