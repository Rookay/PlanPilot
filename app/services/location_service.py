# -*- coding: UTF-8 -*-
"""常用地点的持久化与默认地点约束。"""

from typing import List

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.location import SavedLocation
from app.schemas.location import SavedLocationCreate, SavedLocationUpdate


def list_locations(db: Session) -> List[SavedLocation]:
    return (
        db.query(SavedLocation)
        .order_by(SavedLocation.is_default.desc(), SavedLocation.id.asc())
        .all()
    )


def get_location(db: Session, location_id: int) -> SavedLocation:
    location = db.query(SavedLocation).filter(SavedLocation.id == location_id).first()
    if location is None:
        raise HTTPException(status_code=404, detail="常用地点不存在")
    return location


def create_location(db: Session, payload: SavedLocationCreate) -> SavedLocation:
    _ensure_unique_label(db, payload.label)
    make_default = payload.is_default or db.query(SavedLocation).count() == 0
    if make_default:
        _clear_default(db)
    location = SavedLocation(
        **payload.model_dump(exclude={"is_default"}),
        is_default=make_default,
    )
    db.add(location)
    db.commit()
    db.refresh(location)
    return location


def update_location(
    db: Session,
    location_id: int,
    payload: SavedLocationUpdate,
) -> SavedLocation:
    location = get_location(db, location_id)
    changes = payload.model_dump(exclude_unset=True)
    if "label" in changes:
        _ensure_unique_label(db, changes["label"], exclude_id=location_id)

    requested_default = changes.pop("is_default", None)
    for key, value in changes.items():
        setattr(location, key, value)

    if requested_default is True:
        _clear_default(db)
        location.is_default = True
    elif requested_default is False and location.is_default:
        replacement = (
            db.query(SavedLocation)
            .filter(SavedLocation.id != location_id)
            .order_by(SavedLocation.id.asc())
            .first()
        )
        if replacement is None:
            raise HTTPException(status_code=400, detail="至少需要保留一个默认地点")
        location.is_default = False
        replacement.is_default = True

    db.commit()
    db.refresh(location)
    return location


def delete_location(db: Session, location_id: int) -> dict:
    location = get_location(db, location_id)
    was_default = location.is_default
    # 旧版 SQLite 表没有外键约束，显式清理关联可兼容新旧数据库。
    from app.models.event import Event
    from app.models.task import Task

    db.query(Task).filter(Task.location_id == location_id).update(
        {
            "location_id": None,
            "location": location.label,
            "location_city": location.city,
            "location_longitude": location.longitude,
            "location_latitude": location.latitude,
        },
        synchronize_session=False,
    )
    db.query(Event).filter(Event.location_id == location_id).update(
        {
            "location_id": None,
            "location": location.label,
            "location_city": location.city,
            "location_longitude": location.longitude,
            "location_latitude": location.latitude,
        },
        synchronize_session=False,
    )
    db.query(Event).filter(Event.origin_location_id == location_id).update(
        {
            "origin_location_id": None,
            "origin_location": location.label,
            "origin_location_city": location.city,
            "origin_location_longitude": location.longitude,
            "origin_location_latitude": location.latitude,
        },
        synchronize_session=False,
    )
    db.delete(location)
    db.flush()
    if was_default:
        replacement = db.query(SavedLocation).order_by(SavedLocation.id.asc()).first()
        if replacement is not None:
            replacement.is_default = True
    db.commit()
    return {"deleted": location_id}


def _clear_default(db: Session) -> None:
    db.query(SavedLocation).filter(SavedLocation.is_default.is_(True)).update(
        {"is_default": False},
        synchronize_session=False,
    )


def _ensure_unique_label(
    db: Session,
    label: str,
    exclude_id: int | None = None,
) -> None:
    query = db.query(SavedLocation).filter(func.lower(SavedLocation.label) == label.lower())
    if exclude_id is not None:
        query = query.filter(SavedLocation.id != exclude_id)
    if query.first() is not None:
        raise HTTPException(status_code=409, detail=f"地点名称「{label}」已存在")
