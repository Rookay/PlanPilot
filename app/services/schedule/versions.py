# -*- coding: UTF-8 -*-
"""计划版本：列出快照与撤销最近一次修改。"""

from sqlalchemy.orm import Session

from app.services import plan_version_service


def list_plan_versions(db: Session, limit: int = 20) -> list[dict]:
    return plan_version_service.list_versions(db, limit)


def undo_latest_plan(db: Session) -> dict:
    return plan_version_service.undo_latest(db)
