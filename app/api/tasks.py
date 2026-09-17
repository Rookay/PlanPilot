# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：tasks.py
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.task import TaskCreate, TaskResponse, TaskUpdate
from app.services import task_service

router = APIRouter(
    prefix="/api/tasks",
    tags=["Tasks"],
)


@router.post("", response_model=TaskResponse)
def create_task(
    task_data: TaskCreate,
    db: Session = Depends(get_db),
):
    return task_service.create_task(db, task_data)


@router.get("", response_model=List[TaskResponse])
def get_tasks(
    status: Optional[str] = Query(
        default=None,
        description="按状态筛选，例如 pending / completed",
    ),
    priority: Optional[Literal["low", "medium", "high"]] = Query(default=None),
    category: Optional[str] = Query(default=None, max_length=50),
    q: Optional[str] = Query(default=None, max_length=200),
    sort: Literal["created_desc", "deadline_asc", "duration_asc", "title_asc"] = Query(
        default="created_desc"
    ),
    db: Session = Depends(get_db),
):
    return task_service.list_tasks(db, status, priority, category, q, sort)


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: int,
    db: Session = Depends(get_db),
):
    return task_service.get_task(db, task_id)


@router.put("/{task_id}", response_model=TaskResponse)
def update_task(
    task_id: int,
    task_data: TaskUpdate,
    db: Session = Depends(get_db),
):
    return task_service.update_task(db, task_id, task_data)


@router.delete("/{task_id}")
def delete_task(
    task_id: int,
    db: Session = Depends(get_db),
):
    return task_service.delete_task(db, task_id)
