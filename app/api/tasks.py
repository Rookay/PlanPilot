# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：tasks.py
from typing import List, Optional

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
    db: Session = Depends(get_db),
):
    return task_service.list_tasks(db, status)


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
