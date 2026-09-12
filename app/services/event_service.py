# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：event_service.py
"""事件业务逻辑。API 路由与未来的 Tool 共用。"""

from typing import List

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.event import Event
from app.schemas.event import EventCreate, EventUpdate


def create_event(db: Session, data: EventCreate) -> Event:
    event = Event(**data.model_dump())
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

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(event, key, value)

    db.commit()
    db.refresh(event)
    return event


def delete_event(db: Session, event_id: int) -> dict:
    event = get_event(db, event_id)

    db.delete(event)
    db.commit()

    return {"message": "Event deleted", "id": event_id}
