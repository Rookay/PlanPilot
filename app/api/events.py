# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：events.py
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.event import EventCreate, EventResponse, EventUpdate
from app.services import event_service

router = APIRouter(
    prefix="/api/events",
    tags=["Events"],
)


@router.post("", response_model=EventResponse)
def create_event(
    event_data: EventCreate,
    db: Session = Depends(get_db),
):
    return event_service.create_event(db, event_data)


@router.get("", response_model=List[EventResponse])
def get_events(
    db: Session = Depends(get_db),
):
    return event_service.list_events(db)


@router.get("/{event_id}", response_model=EventResponse)
def get_event(
    event_id: int,
    db: Session = Depends(get_db),
):
    return event_service.get_event(db, event_id)


@router.put("/{event_id}", response_model=EventResponse)
def update_event(
    event_id: int,
    event_data: EventUpdate,
    db: Session = Depends(get_db),
):
    return event_service.update_event(db, event_id, event_data)


@router.delete("/{event_id}")
def delete_event(
    event_id: int,
    db: Session = Depends(get_db),
):
    return event_service.delete_event(db, event_id)
