# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：confirmation.py
"""Confirmation：待确认变更集的暂存与提交。"""

import uuid
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class PendingAction:
    action_id: str
    tool_calls: List[dict] = field(default_factory=list)  # [{name, args}]
    summary: str = ""
    preview: Optional[dict] = None
    session_id: str = ""
    created_at: float = field(default_factory=time.monotonic)


class PendingActionStore:
    """内存版待确认变更集（单进程、单用户够用；多实例可换成 DB 持久化）。"""

    def __init__(self):
        self._store: Dict[str, PendingAction] = {}
        self.ttl_seconds = 600

    def create(
        self,
        tool_calls: List[dict],
        summary: str,
        preview: Optional[dict] = None,
        session_id: str = "",
    ) -> str:
        self._prune()
        action_id = uuid.uuid4().hex[:12]
        self._store[action_id] = PendingAction(
            action_id=action_id,
            tool_calls=tool_calls,
            summary=summary,
            preview=preview,
            session_id=session_id,
        )
        return action_id

    def get(self, action_id: str, session_id: str = "") -> Optional[PendingAction]:
        self._prune()
        action = self._store.get(action_id)
        if action is not None and action.session_id and action.session_id != session_id:
            return None
        return action

    def remove(self, action_id: str) -> None:
        self._store.pop(action_id, None)

    def _prune(self) -> None:
        deadline = time.monotonic() - self.ttl_seconds
        expired = [
            action_id
            for action_id, action in self._store.items()
            if action.created_at < deadline
        ]
        for action_id in expired:
            self._store.pop(action_id, None)
