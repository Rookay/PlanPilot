# -*- coding: UTF-8 -*-
"""Agent 批量确认的补偿式事务快照。"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.plan_version import PlanVersion
from app.models.preference import UserPreference
from app.models.schedule import Schedule
from app.models.task import Task, TaskDependency


_MODELS = (Task, Event, TaskDependency, Schedule, UserPreference, PlanVersion)
_DELETE_ORDER = (Schedule, TaskDependency, Event, Task, UserPreference, PlanVersion)
_RESTORE_ORDER = (Task, Event, TaskDependency, Schedule, UserPreference, PlanVersion)


@dataclass
class AgentDatabaseSnapshot:
    rows: dict[type, list[dict]]

    @classmethod
    def capture(cls, db: Session) -> "AgentDatabaseSnapshot":
        rows = {}
        for model in _MODELS:
            rows[model] = [
                {column.name: getattr(item, column.name) for column in model.__table__.columns}
                for item in db.query(model).all()
            ]
        return cls(rows=rows)

    def restore(self, db: Session) -> None:
        db.rollback()
        for model in _DELETE_ORDER:
            db.query(model).delete(synchronize_session=False)
        db.flush()
        db.expunge_all()
        for model in _RESTORE_ORDER:
            db.add_all(model(**values) for values in self.rows.get(model, []))
            db.flush()
        db.commit()
        db.expire_all()
