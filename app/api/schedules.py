# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：schedules.py
"""日程规划接口。

规划算法在 :mod:`app.planning.engine`，业务逻辑在 :mod:`app.services.schedule_service`。
本文件只做路由绑定。
"""

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.schedule import (
    ScheduleGenerateRequest,
    ScheduleGenerateResponse,
    ScheduleResponse,
    ScheduleUpdate,
)
from app.services import schedule_service

router = APIRouter(
    prefix="/api/schedules",
    tags=["Schedules"],
)


@router.get("", response_model=List[ScheduleResponse])
def list_schedules(db: Session = Depends(get_db)):
    return schedule_service.list_schedules(db)


@router.post("/generate", response_model=ScheduleGenerateResponse)
def generate_schedules(
    payload: ScheduleGenerateRequest,
    db: Session = Depends(get_db),
):
    return schedule_service.generate_schedules(db, payload)


@router.delete("")
def clear_schedules(db: Session = Depends(get_db)):
    """清空全部自动生成的日程提议。"""

    return schedule_service.clear_schedules(db)


@router.patch("/{schedule_id}", response_model=ScheduleResponse)
def update_schedule(
    schedule_id: int,
    payload: ScheduleUpdate,
    db: Session = Depends(get_db),
):
    return schedule_service.update_schedule(db, schedule_id, payload)


@router.delete("/{schedule_id}")
def delete_schedule(
    schedule_id: int,
    db: Session = Depends(get_db),
):
    return schedule_service.delete_schedule(db, schedule_id)
