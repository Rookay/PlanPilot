# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：event_service.py
"""事件业务逻辑，由 API 路由和 Agent Tool 共用。"""

import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.event import Event
from app.schemas.event import (
    EventCreate,
    EventSeriesCreate,
    EventUpdate,
    TripRevision,
    TripSummary,
)
from app.services import location_service, time_policy
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
    values = _normalize_event_times(data.model_dump())
    time_policy.ensure_not_past(values.get("start_time"))
    _validate_location(db, values.get("location_id"))
    _validate_location(db, values.get("origin_location_id"))
    _validate_coordinates(values.get("location_longitude"), values.get("location_latitude"))
    _validate_coordinates(
        values.get("origin_location_longitude"),
        values.get("origin_location_latitude"),
        label="出发地",
    )
    event = Event(**values)
    _validate_time_range(event)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def create_events(
    db: Session,
    items: List[EventCreate],
    trip_title: str | None = None,
    trip_id: str | None = None,
) -> List[Event]:
    """原子创建一组连续行程，任一项失败时不保留半成品。"""

    if not items:
        raise HTTPException(status_code=400, detail="至少需要一个行程项")

    events: List[Event] = []
    group_id = trip_id or uuid.uuid4().hex
    ordered_items = sorted(items, key=lambda item: to_naive_utc(item.start_time))
    inferred_city = next((item.location_city for item in ordered_items if item.location_city), None)
    group_title = trip_title or (
        f"{inferred_city}行程"
        if inferred_city
        else f"{to_naive_utc(ordered_items[0].start_time):%m月%d日}行程"
    )
    try:
        for order, data in enumerate(ordered_items, start=1):
            values = _normalize_event_times(data.model_dump())
            time_policy.ensure_not_past(values.get("start_time"))
            _validate_location(db, values.get("location_id"))
            _validate_location(db, values.get("origin_location_id"))
            _validate_coordinates(
                values.get("location_longitude"), values.get("location_latitude")
            )
            _validate_coordinates(
                values.get("origin_location_longitude"),
                values.get("origin_location_latitude"),
                label="出发地",
            )
            event = Event(
                **values,
                trip_id=group_id,
                trip_title=group_title,
                trip_order=order,
            )
            _validate_time_range(event)
            events.append(event)

        ordered = sorted(events, key=lambda item: item.start_time)
        for previous, current in zip(ordered, ordered[1:]):
            if current.start_time < previous.end_time:
                raise HTTPException(
                    status_code=400,
                    detail=f"行程「{previous.title}」与「{current.title}」时间重叠",
                )

        db.add_all(events)
        db.commit()
        for event in events:
            db.refresh(event)
        return events
    except Exception:
        db.rollback()
        raise


def list_events(
    db: Session,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> List[Event]:
    query = db.query(Event)
    if start is not None:
        query = query.filter(Event.end_time > to_naive_utc(start))
    if end is not None:
        query = query.filter(Event.start_time < to_naive_utc(end))
    return query.order_by(Event.start_time).all()


def list_trips(db: Session) -> List[TripSummary]:
    grouped: dict[str, list[Event]] = {}
    for event in (
        db.query(Event)
        .filter(Event.trip_id.isnot(None))
        .order_by(Event.start_time, Event.id)
        .all()
    ):
        grouped.setdefault(event.trip_id, []).append(event)
    return [
        TripSummary(
            trip_id=trip_id,
            title=events[0].trip_title or "未命名行程",
            start_time=min(item.start_time for item in events),
            end_time=max(item.end_time for item in events),
            event_count=len(events),
            cities=list(
                dict.fromkeys(
                    item.location_city for item in events if item.location_city
                )
            ),
        )
        for trip_id, events in grouped.items()
    ]


def get_trip_events(db: Session, trip_id: str) -> List[Event]:
    events = (
        db.query(Event)
        .filter(Event.trip_id == trip_id)
        .order_by(Event.trip_order, Event.start_time, Event.id)
        .all()
    )
    if not events:
        raise HTTPException(status_code=404, detail="行程组不存在")
    return events


def preview_trip_revision(db: Session, revision: TripRevision) -> dict:
    events = get_trip_events(db, revision.trip_id)
    by_id = {event.id: event for event in events}
    touched_ids = {item.event_id for item in revision.updates}
    delete_ids = set(revision.delete_event_ids)
    unknown = sorted((touched_ids | delete_ids) - set(by_id))
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"以下事件不属于该行程组：{', '.join(map(str, unknown))}",
        )
    if touched_ids & delete_ids:
        raise HTTPException(status_code=400, detail="同一事件不能同时修改和删除")

    update_by_id = {item.event_id: item for item in revision.updates}
    after: list[tuple[int, EventCreate]] = []
    for event in events:
        if event.id in delete_ids:
            continue
        values = {
            name: getattr(event, name)
            for name in EventCreate.model_fields
        }
        if revision.shift_minutes:
            delta = timedelta(minutes=revision.shift_minutes)
            values["start_time"] = event.start_time + delta
            values["end_time"] = event.end_time + delta
        update = update_by_id.get(event.id)
        if update is not None:
            changes = update.model_dump(exclude={"event_id"}, exclude_unset=True)
            if (
                "location" in changes
                and changes["location"] != event.location
                and "location_longitude" not in changes
                and "location_latitude" not in changes
                and not changes.get("location_id")
            ):
                changes.update(
                    location_city=None,
                    location_longitude=None,
                    location_latitude=None,
                )
            if (
                "origin_location" in changes
                and changes["origin_location"] != event.origin_location
                and "origin_location_longitude" not in changes
                and "origin_location_latitude" not in changes
                and not changes.get("origin_location_id")
            ):
                changes.update(
                    origin_location_city=None,
                    origin_location_longitude=None,
                    origin_location_latitude=None,
                )
            values.update(changes)
        values = _normalize_event_times(values)
        original = by_id[event.id]
        if values["start_time"] != original.start_time:
            time_policy.ensure_not_past(values["start_time"])
        after.append((event.id, EventCreate(**values)))

    if not after:
        raise HTTPException(status_code=400, detail="行程组至少需要保留一个事件")
    after.sort(key=lambda item: item[1].start_time)
    for (_, previous), (_, current) in zip(after, after[1:]):
        if current.start_time < previous.end_time:
            raise HTTPException(
                status_code=400,
                detail=f"行程「{previous.title}」与「{current.title}」时间重叠",
            )
    return {
        "trip_id": revision.trip_id,
        "trip_title": revision.trip_title or events[0].trip_title or "未命名行程",
        "before": events,
        "after": after,
        "deleted_ids": sorted(delete_ids),
        "shift_minutes": revision.shift_minutes,
    }


def apply_trip_revision(db: Session, revision: TripRevision) -> List[Event]:
    preview = preview_trip_revision(db, revision)
    by_id = {event.id: event for event in preview["before"]}
    try:
        for event_id in preview["deleted_ids"]:
            db.delete(by_id[event_id])
        for order, (event_id, payload) in enumerate(preview["after"], start=1):
            event = by_id[event_id]
            values = _normalize_event_times(payload.model_dump())
            _validate_location(db, values.get("location_id"))
            _validate_location(db, values.get("origin_location_id"))
            for key, value in values.items():
                setattr(event, key, value)
            event.trip_title = preview["trip_title"]
            event.trip_order = order
            _validate_time_range(event)
            _validate_coordinates(event.location_longitude, event.location_latitude)
            _validate_coordinates(
                event.origin_location_longitude,
                event.origin_location_latitude,
                label="出发地",
            )
        db.commit()
        result = get_trip_events(db, revision.trip_id)
        for event in result:
            db.refresh(event)
        return result
    except Exception:
        db.rollback()
        raise


def create_event_series(db: Session, data: EventSeriesCreate) -> List[Event]:
    """把重复规则展开为独立事件，后续每次仍可单独调整。"""

    common = data.model_dump(exclude={"recurrence", "occurrences", "start_time", "end_time"})
    current_start = to_naive_utc(data.start_time)
    current_end = to_naive_utc(data.end_time)
    time_policy.ensure_not_past(current_start)
    created: List[Event] = []
    _validate_location(db, common.get("location_id"))
    _validate_location(db, common.get("origin_location_id"))
    _validate_coordinates(common.get("location_longitude"), common.get("location_latitude"))
    _validate_coordinates(
        common.get("origin_location_longitude"),
        common.get("origin_location_latitude"),
        label="出发地",
    )

    if data.recurrence == "weekdays":
        while current_start.weekday() >= 5:
            current_start += timedelta(days=1)
            current_end += timedelta(days=1)

    for _ in range(data.occurrences):
        event = Event(**common, start_time=current_start, end_time=current_end)
        db.add(event)
        created.append(event)
        step = 7 if data.recurrence == "weekly" else 1
        current_start += timedelta(days=step)
        current_end += timedelta(days=step)
        if data.recurrence == "weekdays":
            while current_start.weekday() >= 5:
                current_start += timedelta(days=1)
                current_end += timedelta(days=1)

    db.commit()
    for event in created:
        db.refresh(event)
    return created


def get_event(db: Session, event_id: int) -> Event:
    event = db.query(Event).filter(Event.id == event_id).first()

    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")

    return event


def update_event(db: Session, event_id: int, data: EventUpdate) -> Event:
    event = get_event(db, event_id)

    update_data = _normalize_event_times(data.model_dump(exclude_unset=True))
    if "start_time" in update_data:
        time_policy.ensure_not_past(update_data["start_time"])
    if "location_id" in update_data:
        _validate_location(db, update_data["location_id"])
    if "origin_location_id" in update_data:
        _validate_location(db, update_data["origin_location_id"])
    if (
        "location" in update_data
        and update_data["location"] != event.location
        and "location_longitude" not in update_data
        and "location_latitude" not in update_data
        and not update_data.get("location_id")
    ):
        event.location_longitude = None
        event.location_latitude = None
        event.location_city = None
    if (
        "origin_location" in update_data
        and update_data["origin_location"] != event.origin_location
        and "origin_location_longitude" not in update_data
        and "origin_location_latitude" not in update_data
        and not update_data.get("origin_location_id")
    ):
        event.origin_location_longitude = None
        event.origin_location_latitude = None
        event.origin_location_city = None
    for key, value in update_data.items():
        setattr(event, key, value)
    if event.event_type == "travel" and event.travel_mode is None:
        event.travel_mode = "transit"
    if event.event_type != "travel":
        event.travel_mode = None
    _validate_coordinates(event.location_longitude, event.location_latitude)
    _validate_coordinates(
        event.origin_location_longitude,
        event.origin_location_latitude,
        label="出发地",
    )

    _validate_time_range(event)

    if event.trip_id:
        db.flush()
        _reindex_trip(db, event.trip_id)
    db.commit()
    db.refresh(event)
    return event


def delete_event(db: Session, event_id: int) -> dict:
    event = get_event(db, event_id)
    trip_id = event.trip_id

    db.delete(event)
    db.flush()
    if trip_id:
        _reindex_trip(db, trip_id)
    db.commit()

    return {"message": "Event deleted", "id": event_id}


def delete_events(db: Session, event_ids: List[int]) -> dict:
    """原子地删除一组事件；任一 id 不存在时不执行删除。"""

    unique_ids = list(dict.fromkeys(event_ids))
    if not unique_ids:
        raise HTTPException(status_code=400, detail="至少选择一个要删除的事件")

    events = db.query(Event).filter(Event.id.in_(unique_ids)).all()
    by_id = {event.id: event for event in events}
    missing = [event_id for event_id in unique_ids if event_id not in by_id]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"以下事件不存在：{', '.join(map(str, missing))}",
        )

    titles = [by_id[event_id].title for event_id in unique_ids]
    trip_ids = {event.trip_id for event in events if event.trip_id}
    db.query(Event).filter(Event.id.in_(unique_ids)).delete(synchronize_session=False)
    db.flush()
    for trip_id in trip_ids:
        _reindex_trip(db, trip_id)
    db.commit()
    return {
        "message": "Events deleted",
        "ids": unique_ids,
        "titles": titles,
        "deleted": len(unique_ids),
    }


def delete_trip(db: Session, trip_id: str) -> dict:
    """Atomically delete every event in one explicit trip group."""

    events = get_trip_events(db, trip_id)
    title = events[0].trip_title or "未命名日程组"
    result = delete_events(db, [event.id for event in events])
    return {
        **result,
        "trip_id": trip_id,
        "trip_title": title,
    }


def _validate_location(db: Session, location_id: int | None) -> None:
    if location_id is not None:
        location_service.get_location(db, location_id)


def _validate_coordinates(
    longitude: float | None,
    latitude: float | None,
    label: str = "地点",
) -> None:
    if (longitude is None) != (latitude is None):
        raise HTTPException(status_code=400, detail=f"{label}经度和纬度必须同时提供")


def _reindex_trip(db: Session, trip_id: str) -> None:
    events = (
        db.query(Event)
        .filter(Event.trip_id == trip_id)
        .order_by(Event.start_time, Event.end_time, Event.id)
        .all()
    )
    for order, event in enumerate(events, start=1):
        event.trip_order = order
