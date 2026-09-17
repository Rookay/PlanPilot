# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：memory.py
"""Memory：短期会话 + 长期偏好（第一版）。

- 短期：按 session 保存在内存中的对话历史。
- 长期：用户偏好，保存在 user_preferences 表。
"""

from typing import Dict, List

from sqlalchemy.orm import Session

from app.schemas.preference import PreferenceUpdate
from app.services import preference_service


class Memory:
    def __init__(self):
        self._sessions: Dict[str, List[dict]] = {}

    # ---- 短期会话 ----

    def add(self, session_id: str, role: str, content: str) -> None:
        self.add_message(session_id, {"role": role, "content": content})

    def add_message(self, session_id: str, message: dict) -> None:
        history = self._sessions.setdefault(session_id, [])
        history.append(message)
        if len(history) > 100:
            del history[:-100]

    def recent(self, session_id: str, limit: int = 20) -> List[dict]:
        history = self._sessions.get(session_id, [])
        return history[-limit:]

    def clear(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def has_session(self, session_id: str) -> bool:
        return session_id in self._sessions

    def hydrate(self, session_id: str, messages: List[dict]) -> None:
        """仅在内存会话不存在时恢复持久化历史。"""

        if not self.has_session(session_id):
            self._sessions[session_id] = list(messages)

    # ---- 长期偏好 ----

    def get_preferences(self, db: Session) -> dict:
        return preference_service.get_preferences(db).model_dump()

    def set_preference(self, db: Session, key: str, value: str) -> None:
        preference_service.update_preferences(db, PreferenceUpdate(**{key: value}))
