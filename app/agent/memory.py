# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：memory.py
"""Memory：短期会话 + 长期偏好（第一版）。

- 短期：按 session 保存在内存中的对话历史。
- 长期：用户偏好，保存在 user_preferences 表。

并发模型：
FastAPI 同步 endpoint 通过 anyio 线程池执行，多个请求可以同时落到同一个
``Memory`` 实例上。所有对 ``_sessions`` 的读写都通过 ``threading.Lock`` 串行化，
避免 ``RuntimeError: dictionary changed size during iteration`` 或裁剪错位。
锁粒度有意粗一些——Memory 调用很轻，开销主要在 LLM 调用，锁的影响可以忽略。
"""

import threading
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.schemas.preference import PreferenceUpdate
from app.services import preference_service

# 单个 session 最多保留的最近消息条数，超出后从头部裁剪。
_HISTORY_MAX = 100


class Memory:
    def __init__(self) -> None:
        self._sessions: Dict[str, List[dict]] = {}
        self._lock = threading.Lock()

    # ---- 内部辅助 ----

    def _locked_history(self, session_id: str) -> List[dict]:
        """返回 session 当前历史列表的引用，调用方必须在持锁期间使用。"""

        return self._sessions.setdefault(session_id, [])

    # ---- 短期会话 ----

    def add(self, session_id: str, role: str, content: str) -> None:
        self.add_message(session_id, {"role": role, "content": content})

    def add_message(self, session_id: str, message: dict) -> None:
        with self._lock:
            history = self._locked_history(session_id)
            history.append(message)
            if len(history) > _HISTORY_MAX:
                del history[:-_HISTORY_MAX]

    def recent(self, session_id: str, limit: int = 20) -> List[dict]:
        with self._lock:
            history = self._locked_history(session_id)
            return list(history[-limit:])

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def replace_last_assistant(self, session_id: str, content: str) -> None:
        """同步 Agent 在执行后审计阶段补充的最终回复。"""

        with self._lock:
            for message in reversed(self._locked_history(session_id)):
                if message.get("role") == "assistant":
                    message["content"] = content
                    return

    def has_session(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._sessions

    def hydrate(self, session_id: str, messages: List[dict]) -> None:
        """仅在内存会话不存在时恢复持久化历史。"""

        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = list(messages)

    def size(self, session_id: Optional[str] = None) -> int:
        """测试用辅助：返回指定 session 历史长度，或全部 session 数。"""

        with self._lock:
            if session_id is None:
                return len(self._sessions)
            history = self._sessions.get(session_id)
            return len(history) if history is not None else 0

    # ---- 长期偏好 ----

    def get_preferences(self, db: Session) -> dict:
        return preference_service.get_preferences(db).model_dump()

    def set_preference(self, db: Session, key: str, value: str) -> None:
        preference_service.update_preferences(db, PreferenceUpdate(**{key: value}))
