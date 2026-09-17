# -*- coding: UTF-8 -*-
"""把用户本地时间偏好转换为规划引擎使用的 UTC 窗口。"""

from datetime import date, datetime, time, timedelta, timezone
from typing import List, Tuple
from zoneinfo import ZoneInfo

from app.schemas.preference import PreferenceSettings

IntervalTuple = Tuple[datetime, datetime]


def build_local_work_windows(
    start_utc: datetime,
    days: int,
    preferences: PreferenceSettings,
) -> List[IntervalTuple]:
    zone = ZoneInfo(preferences.timezone)
    local_start = start_utc.replace(tzinfo=timezone.utc).astimezone(zone)
    work_start = time.fromisoformat(preferences.workday_start)
    work_end = time.fromisoformat(preferences.workday_end)
    windows: List[IntervalTuple] = []

    for offset in range(days):
        day = local_start.date() + timedelta(days=offset)
        if not preferences.schedule_weekends and day.weekday() >= 5:
            continue
        local_window_start = datetime.combine(day, work_start, zone)
        local_window_end = datetime.combine(day, work_end, zone)
        start = _as_naive_utc(local_window_start)
        end = _as_naive_utc(local_window_end)
        start = max(start, start_utc)
        if end > start:
            windows.append((start, end))
    return windows


def lunch_intervals(
    windows: List[IntervalTuple],
    preferences: PreferenceSettings,
) -> List[IntervalTuple]:
    if not preferences.lunch_start or not preferences.lunch_end:
        return []
    return _local_period_windows(
        windows,
        preferences,
        time.fromisoformat(preferences.lunch_start),
        time.fromisoformat(preferences.lunch_end),
    )


def preferred_windows(
    preferred_time: str | None,
    windows: List[IntervalTuple],
    preferences: PreferenceSettings,
) -> List[IntervalTuple]:
    if not preferred_time:
        return []

    text = preferred_time.strip().lower()
    if any(word in text for word in ("上午", "早上", "morning")):
        start, end = time(8), time(12)
    elif any(word in text for word in ("下午", "afternoon")):
        start, end = time(13), time(18)
    elif any(word in text for word in ("晚上", "晚间", "evening")):
        start, end = time(18), time(23, 59)
    else:
        try:
            exact = time.fromisoformat(text)
        except ValueError:
            return []
        anchor = datetime.combine(date(2000, 1, 2), exact)
        start = (anchor - timedelta(hours=1)).time()
        end = (anchor + timedelta(hours=2)).time()

    return _local_period_windows(windows, preferences, start, end)


def _local_period_windows(
    windows: List[IntervalTuple],
    preferences: PreferenceSettings,
    period_start: time,
    period_end: time,
) -> List[IntervalTuple]:
    zone = ZoneInfo(preferences.timezone)
    days = {
        moment.replace(tzinfo=timezone.utc).astimezone(zone).date()
        for window in windows
        for moment in window
    }
    periods: List[IntervalTuple] = []
    for day in sorted(days):
        local_start = datetime.combine(day, period_start, zone)
        local_end = datetime.combine(day, period_end, zone)
        if local_end <= local_start:
            local_end += timedelta(days=1)
        periods.append((_as_naive_utc(local_start), _as_naive_utc(local_end)))
    return periods


def _as_naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)
