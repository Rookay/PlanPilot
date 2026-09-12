# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：utils.py
"""项目通用工具函数。"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    """返回不带时区信息的 UTC 当前时间。

    SQLite 的 DateTime 列不保存时区，因此项目内统一使用 "naive UTC"，
    这样既能避免 ``datetime.utcnow()`` 在 Python 3.12+ 的弃用告警，
    又能保证与前端 ``toISOString()``（带 Z 的 UTC 时间）一致。
    """

    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(value: datetime | None) -> datetime | None:
    """把可能带时区的 datetime 统一转换为不带时区的 UTC 时间。"""

    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
