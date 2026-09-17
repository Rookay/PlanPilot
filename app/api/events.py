# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：events.py
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.event import (
    EventCreate,
    EventResponse,
    EventSeriesCreate,
    EventSeriesResponse,
    EventUpdate,
    TripSummary,
)
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
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return event_service.list_events(db, start, end)


@router.post("/series", response_model=EventSeriesResponse)
def create_event_series(
    event_data: EventSeriesCreate,
    db: Session = Depends(get_db),
):
    events = event_service.create_event_series(db, event_data)
    return EventSeriesResponse(events=events, created=len(events))


@router.get("/trips", response_model=List[TripSummary])
def get_trips(db: Session = Depends(get_db)):
    return event_service.list_trips(db)


@router.get("/trips/{trip_id}", response_model=List[EventResponse])
def get_trip(trip_id: str, db: Session = Depends(get_db)):
    return event_service.get_trip_events(db, trip_id)


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
