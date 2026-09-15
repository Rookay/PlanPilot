# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：event_service.py
"""事件业务逻辑。API 路由与未来的 Tool 共用。"""

from typing import List

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.event import Event
from app.schemas.event import EventCreate, EventUpdate
from app.utils import to_naive_utc


def _normalize_event_times(data: dict) -> dict:
    for field in ("start_time", "end_time"):
        if field in data:
            data[field] = to_naive_utc(data[field])
    return data


def _validate_time_range(event: Event) -> None:
    if event.end_time <= event.start_time:
        raise HTTPException(status_code=400, detail="结束时间必须晚于开始时间")


def create_event(db: Session, data: EventCreate) -> Event:
    event = Event(**_normalize_event_times(data.model_dump()))
    _validate_time_range(event)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def list_events(db: Session) -> List[Event]:
    return db.query(Event).order_by(Event.start_time).all()


def get_event(db: Session, event_id: int) -> Event:
    event = db.query(Event).filter(Event.id == event_id).first()

    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")

    return event


def update_event(db: Session, event_id: int, data: EventUpdate) -> Event:
    event = get_event(db, event_id)

    update_data = _normalize_event_times(data.model_dump(exclude_unset=True))
    for key, value in update_data.items():
        setattr(event, key, value)

    _validate_time_range(event)

    db.commit()
    db.refresh(event)
    return event


def delete_event(db: Session, event_id: int) -> dict:
    event = get_event(db, event_id)

    db.delete(event)
    db.commit()

    return {"message": "Event deleted", "id": event_id}
