# -*- coding: UTF-8 -*-
"""长期事实接口：查看候选、人工确认 / 否决、删除。

为什么需要这几个人工入口：事实是模型归纳的，误判一定会有（把玩笑当成偏好、
把一次性的临时安排当成固定习惯）。没有否决入口，用户只能看着 system prompt
里一直挂着一条错信息，而他又看不到 prompt——这才是最糟的状态。
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.fact import FactStatusUpdate, MemoryFactResponse
from app.services import fact_service

router = APIRouter(prefix="/api/memory/facts", tags=["Memory"])


@router.get("", response_model=List[MemoryFactResponse])
def list_facts(
    status: Optional[str] = Query(default=None, description="candidate / confirmed / rejected"),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return fact_service.list_facts(db, status=status, limit=limit)


@router.patch("/{fact_id}", response_model=MemoryFactResponse)
def update_status(
    fact_id: int,
    payload: FactStatusUpdate,
    db: Session = Depends(get_db),
):
    record = fact_service.set_status(db, fact_id, payload.status)
    if record is None:
        raise HTTPException(status_code=404, detail="事实不存在")
    db.commit()
    return record


@router.delete("/{fact_id}")
def delete_fact(fact_id: int, db: Session = Depends(get_db)):
    if not fact_service.delete_fact(db, fact_id):
        raise HTTPException(status_code=404, detail="事实不存在")
    db.commit()
    return {"deleted": 1}
