# -*- coding: UTF-8 -*-
"""计划版本快照、差异和最近操作撤销。"""

import json
import uuid
from datetime import datetime
from typing import Iterable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.plan_version import PlanVersion
from app.models.schedule import Schedule
from app.models.task import Task
from app.utils import utcnow


_SCHEDULE_FIELDS = (
    "id",
    "task_id",
    "start_time",
    "end_time",
    "status",
    "plan_batch_id",
    "plan_version_id",
    "reason",
    "is_locked",
    "chunk_index",
    "source",
    "created_at",
    "updated_at",
)
_DATETIME_FIELDS = {"start_time", "end_time", "created_at", "updated_at"}


def begin_version(db: Session, action: str, summary: str) -> PlanVersion:
    version = PlanVersion(
        id=uuid.uuid4().hex[:16],
        action=action,
        summary=summary,
        before_snapshot=json.dumps(_snapshot(db), ensure_ascii=False),
        changes="[]",
    )
    db.add(version)
    db.flush()
    return version


def finalize_version(db: Session, version: PlanVersion) -> list[dict]:
    before = json.loads(version.before_snapshot)
    changes = _schedule_changes(before.get("schedules", []), _snapshot(db)["schedules"])
    version.changes = json.dumps(changes, ensure_ascii=False)
    return changes


def list_versions(db: Session, limit: int = 20) -> list[dict]:
    rows = (
        db.query(PlanVersion)
        .order_by(PlanVersion.created_at.desc())
        .limit(limit)
        .all()
    )
    return [_version_dict(item) for item in rows]


def undo_latest(db: Session) -> dict:
    version = (
        db.query(PlanVersion)
        .filter(PlanVersion.status == "active")
        .order_by(PlanVersion.created_at.desc())
        .first()
    )
    if version is None:
        raise HTTPException(status_code=404, detail="没有可撤销的计划修改")

    snapshot = json.loads(version.before_snapshot)
    version_id = version.id
    db.query(Schedule).delete(synchronize_session=False)
    db.flush()
    db.expunge_all()
    for values in snapshot.get("schedules", []):
        restored = dict(values)
        for field in _DATETIME_FIELDS:
            if restored.get(field):
                restored[field] = datetime.fromisoformat(restored[field])
        db.add(Schedule(**restored))

    db.flush()
    task_statuses = {
        int(item["id"]): item["status"] for item in snapshot.get("tasks", [])
    }
    for task in db.query(Task).filter(Task.id.in_(task_statuses)).all():
        task.status = task_statuses[task.id]

    restored_version = db.get(PlanVersion, version_id)
    restored_version.status = "undone"
    restored_version.undone_at = utcnow()
    db.commit()
    return {
        "version": _version_dict(restored_version),
        "restored_schedules": len(snapshot.get("schedules", [])),
    }


def _snapshot(db: Session) -> dict:
    schedules = []
    for schedule in db.query(Schedule).order_by(Schedule.id).all():
        values = {}
        for field in _SCHEDULE_FIELDS:
            value = getattr(schedule, field)
            values[field] = value.isoformat() if isinstance(value, datetime) else value
        schedules.append(values)
    tasks = [
        {"id": task.id, "status": task.status}
        for task in db.query(Task).order_by(Task.id).all()
    ]
    return {"schedules": schedules, "tasks": tasks}


def _schedule_changes(before: Iterable[dict], after: Iterable[dict]) -> list[dict]:
    old = {int(item["id"]): item for item in before}
    new = {int(item["id"]): item for item in after}
    changes: list[dict] = []
    for schedule_id in sorted(old.keys() - new.keys()):
        changes.append({"type": "removed", "schedule": old[schedule_id]})
    for schedule_id in sorted(new.keys() - old.keys()):
        changes.append({"type": "added", "schedule": new[schedule_id]})
    tracked = ("start_time", "end_time", "status", "is_locked", "chunk_index")
    for schedule_id in sorted(old.keys() & new.keys()):
        fields = {
            field: {"before": old[schedule_id].get(field), "after": new[schedule_id].get(field)}
            for field in tracked
            if old[schedule_id].get(field) != new[schedule_id].get(field)
        }
        if fields:
            changes.append(
                {"type": "updated", "schedule_id": schedule_id, "fields": fields}
            )
    return changes


def _version_dict(version: PlanVersion) -> dict:
    return {
        "id": version.id,
        "action": version.action,
        "summary": version.summary,
        "status": version.status,
        "changes": json.loads(version.changes or "[]"),
        "created_at": version.created_at,
        "undone_at": version.undone_at,
    }
