# -*- coding: UTF-8 -*-
# Project ：mine 
# File    ：events.py
# IDE     ：PyCharm 
# Author  ：刘景涛
# Date    ：2026/9/11 14:14
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.event import Event
from app.schemas.event import (
    EventCreate,
    EventResponse,
    EventUpdate,
)

router = APIRouter(
    prefix="/api/events",
    tags=["Events"],
)


@router.post(
    "",
    response_model=EventResponse,
)
def create_event(
    event_data: EventCreate,
    db: Session = Depends(get_db),
):
    event = Event(**event_data.model_dump())

    db.add(event)
    db.commit()
    db.refresh(event)

    return event


@router.get(
    "",
    response_model=List[EventResponse],
)
def get_events(
    db: Session = Depends(get_db),
):
    return (
        db.query(Event)
        .order_by(Event.start_time)
        .all()
    )


@router.get(
    "/{event_id}",
    response_model=EventResponse,
)
def get_event(
    event_id: int,
    db: Session = Depends(get_db),
):
    event = (
        db.query(Event)
        .filter(Event.id == event_id)
        .first()
    )

    if event is None:
        raise HTTPException(
            status_code=404,
            detail="Event not found",
        )

    return event


@router.put(
    "/{event_id}",
    response_model=EventResponse,
)
def update_event(
    event_id: int,
    event_data: EventUpdate,
    db: Session = Depends(get_db),
):
    event = (
        db.query(Event)
        .filter(Event.id == event_id)
        .first()
    )

    if event is None:
        raise HTTPException(
            status_code=404,
            detail="Event not found",
        )

    update_data = event_data.model_dump(
        exclude_unset=True
    )

    for key, value in update_data.items():
        setattr(event, key, value)

    db.commit()
    db.refresh(event)

    return event


@router.delete("/{event_id}")
def delete_event(
    event_id: int,
    db: Session = Depends(get_db),
):
    event = (
        db.query(Event)
        .filter(Event.id == event_id)
        .first()
    )

    if event is None:
        raise HTTPException(
            status_code=404,
            detail="Event not found",
        )

    db.delete(event)
    db.commit()

    return {
        "message": "Event deleted",
        "id": event_id,
    }