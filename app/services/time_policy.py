# -*- coding: UTF-8 -*-
"""所有写入入口共用的时间边界规则。"""

from datetime import datetime, timedelta

from fastapi import HTTPException

from app.utils import to_naive_utc, utcnow


SUBMISSION_GRACE = timedelta(minutes=1)


def current_boundary() -> datetime:
    """返回允许提交的最早 UTC 时间，并容忍短暂的网络与确认延迟。"""

    return utcnow() - SUBMISSION_GRACE


def ensure_not_past(value: datetime | None, label: str = "开始时间") -> None:
    normalized = to_naive_utc(value)
    if normalized is not None and normalized < current_boundary():
        raise HTTPException(status_code=400, detail=f"{label}不能早于当前时间")


def clamp_to_now(value: datetime | None) -> datetime:
    """自动规划遇到过去的起点时从当前时刻继续，而不是生成历史日程。"""

    now = utcnow()
    normalized = to_naive_utc(value)
    return max(normalized, now) if normalized is not None else now
