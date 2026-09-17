# -*- coding: UTF-8 -*-
"""用户规划偏好接口。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.preference import PreferenceSettings, PreferenceUpdate
from app.services import preference_service

router = APIRouter(prefix="/api/preferences", tags=["Preferences"])


@router.get("", response_model=PreferenceSettings)
def get_preferences(db: Session = Depends(get_db)):
    return preference_service.get_preferences(db)


@router.put("", response_model=PreferenceSettings)
def update_preferences(payload: PreferenceUpdate, db: Session = Depends(get_db)):
    return preference_service.update_preferences(db, payload)
