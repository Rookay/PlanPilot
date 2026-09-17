# -*- coding: UTF-8 -*-
"""用户偏好服务：提供类型化配置并兼容旧版键值。"""

from sqlalchemy.orm import Session

from app.models.preference import UserPreference
from app.schemas.preference import PreferenceSettings, PreferenceUpdate

DEFAULT_PREFERENCES = PreferenceSettings()


def _rows(db: Session) -> dict[str, str]:
    return {row.key: row.value for row in db.query(UserPreference).all()}


def get_preferences(db: Session) -> PreferenceSettings:
    stored = _rows(db)
    work_start, work_end = _split_range(stored.get("work_hours"), "09:00", "18:00")
    lunch_start, lunch_end = _split_range(stored.get("lunch_time"), "12:00", "13:00")
    data = DEFAULT_PREFERENCES.model_dump()
    data.update(
        {
            "timezone": stored.get("timezone", data["timezone"]),
            "default_location": stored.get("default_location", data["default_location"]),
            "workday_start": stored.get("workday_start", work_start),
            "workday_end": stored.get("workday_end", work_end),
            "lunch_start": stored.get("lunch_start", lunch_start),
            "lunch_end": stored.get("lunch_end", lunch_end),
            "default_task_duration_min": _as_int(
                stored.get("default_task_duration_min"), data["default_task_duration_min"]
            ),
            "default_gap_minutes": _as_int(
                stored.get("default_gap_minutes"), data["default_gap_minutes"]
            ),
            "planning_strategy": stored.get("planning_strategy", data["planning_strategy"]),
            "schedule_weekends": _as_bool(
                stored.get("schedule_weekends"), data["schedule_weekends"]
            ),
        }
    )
    try:
        return PreferenceSettings(**data)
    except ValueError:
        return DEFAULT_PREFERENCES.model_copy()


def update_preferences(db: Session, payload: PreferenceUpdate) -> PreferenceSettings:
    merged = get_preferences(db).model_dump()
    merged.update(payload.model_dump(exclude_unset=True))
    validated = PreferenceSettings(**merged)

    for key, value in validated.model_dump().items():
        row = db.query(UserPreference).filter(UserPreference.key == key).first()
        serialized = str(value).lower() if isinstance(value, bool) else str(value or "")
        if row is None:
            db.add(UserPreference(key=key, value=serialized))
        else:
            row.value = serialized
    db.commit()
    return validated


def _split_range(value: str | None, default_start: str, default_end: str) -> tuple[str, str]:
    if value and "-" in value:
        start, end = value.split("-", 1)
        return start.strip(), end.strip()
    return default_start, default_end


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}
