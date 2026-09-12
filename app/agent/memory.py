# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：memory.py
"""Memory：短期会话 + 长期偏好（第一版）。

- 短期：按 session 保存在内存中的对话历史。
- 长期：用户偏好，保存在 user_preferences 表。
"""

from typing import Dict, List

from sqlalchemy.orm import Session

from app.models.preference import UserPreference

DEFAULT_PREFERENCES = {
    "work_hours": "09:00-18:00",
    "lunch_time": "12:00-13:00",
    "default_task_duration_min": "60",
    "timezone": "Asia/Shanghai",
}


class Memory:
    def __init__(self):
        self._sessions: Dict[str, List[dict]] = {}

    # ---- 短期会话 ----

    def add(self, session_id: str, role: str, content: str) -> None:
        self.add_message(session_id, {"role": role, "content": content})

    def add_message(self, session_id: str, message: dict) -> None:
        self._sessions.setdefault(session_id, []).append(message)

    def recent(self, session_id: str, limit: int = 20) -> List[dict]:
        history = self._sessions.get(session_id, [])
        return history[-limit:]

    def clear(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    # ---- 长期偏好 ----

    def get_preferences(self, db: Session) -> dict:
        prefs = dict(DEFAULT_PREFERENCES)
        for row in db.query(UserPreference).all():
            prefs[row.key] = row.value
        return prefs

    def set_preference(self, db: Session, key: str, value: str) -> None:
        row = db.query(UserPreference).filter(UserPreference.key == key).first()
        if row is None:
            db.add(UserPreference(key=key, value=value))
        else:
            row.value = value
        db.commit()
