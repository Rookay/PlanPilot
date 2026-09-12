# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：confirmation.py
"""Confirmation：待确认变更集的暂存与提交。"""

import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class PendingAction:
    action_id: str
    tool_calls: List[dict] = field(default_factory=list)  # [{name, args}]
    summary: str = ""
    preview: Optional[dict] = None


class PendingActionStore:
    """内存版待确认变更集（单进程、单用户够用；多实例可换成 DB 持久化）。"""

    def __init__(self):
        self._store: Dict[str, PendingAction] = {}

    def create(self, tool_calls: List[dict], summary: str, preview: Optional[dict] = None) -> str:
        action_id = uuid.uuid4().hex[:12]
        self._store[action_id] = PendingAction(
            action_id=action_id,
            tool_calls=tool_calls,
            summary=summary,
            preview=preview,
        )
        return action_id

    def get(self, action_id: str) -> Optional[PendingAction]:
        return self._store.get(action_id)

    def remove(self, action_id: str) -> None:
        self._store.pop(action_id, None)
