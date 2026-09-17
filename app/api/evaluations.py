# -*- coding: UTF-8 -*-
"""Agent 回归评测接口。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import evaluation_service

router = APIRouter(prefix="/api/evaluations", tags=["Evaluations"])


@router.get("/scenarios")
def scenarios():
    return evaluation_service.list_scenarios()


@router.get("/runs")
def runs(limit: int = 20, db: Session = Depends(get_db)):
    return evaluation_service.list_runs(db, limit)


@router.get("/latest")
def latest(db: Session = Depends(get_db)):
    return evaluation_service.latest_run(db)


@router.post("/run")
def run(db: Session = Depends(get_db)):
    return evaluation_service.run_evaluation(db)
