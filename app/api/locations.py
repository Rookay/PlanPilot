# -*- coding: UTF-8 -*-
"""常用地点管理接口。"""

from typing import List

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.location import (
    SavedLocationCreate,
    SavedLocationResponse,
    SavedLocationUpdate,
)
from app.services import location_service

router = APIRouter(prefix="/api/locations", tags=["Locations"])


@router.get("", response_model=List[SavedLocationResponse])
def list_locations(db: Session = Depends(get_db)):
    return location_service.list_locations(db)


@router.post("", response_model=SavedLocationResponse, status_code=status.HTTP_201_CREATED)
def create_location(payload: SavedLocationCreate, db: Session = Depends(get_db)):
    return location_service.create_location(db, payload)


@router.get("/{location_id}", response_model=SavedLocationResponse)
def get_location(location_id: int, db: Session = Depends(get_db)):
    return location_service.get_location(db, location_id)


@router.put("/{location_id}", response_model=SavedLocationResponse)
def update_location(
    location_id: int,
    payload: SavedLocationUpdate,
    db: Session = Depends(get_db),
):
    return location_service.update_location(db, location_id, payload)


@router.delete("/{location_id}")
def delete_location(location_id: int, db: Session = Depends(get_db)):
    return location_service.delete_location(db, location_id)
