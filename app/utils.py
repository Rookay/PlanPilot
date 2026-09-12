# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：utils.py
"""项目通用工具函数。"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


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


def parse_naive_utc(value) -> datetime | None:
    """把字符串/datetime 解析为不带时区的 UTC 时间。

    支持带 ``Z`` 或 ``+HH:MM`` 时区后缀的 ISO 8601 字符串；解析失败返回 None。
    供 Tool 解析 LLM 传入的时间参数使用。
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        return to_naive_utc(value)

    text = str(value).strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    return to_naive_utc(parsed)


def local_now(timezone_name: str) -> datetime:
    """返回指定时区的当前本地时间（去掉 tzinfo，仅表示本地时刻）。

    时区名无效时退回 UTC。
    """

    try:
        return datetime.now(ZoneInfo(timezone_name)).replace(tzinfo=None)
    except Exception:
        return utcnow()


def utc_offset_hours(timezone_name: str) -> int:
    """返回指定时区相对 UTC 的偏移小时数（例如 Asia/Shanghai -> 8）。"""

    try:
        offset = datetime.now(ZoneInfo(timezone_name)).utcoffset()
        return int(offset.total_seconds() // 3600) if offset else 0
    except Exception:
        return 0
