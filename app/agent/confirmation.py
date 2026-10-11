# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：confirmation.py
"""Confirmation:待确认变更集的持久化存储与提交。

落库到 ``pending_actions`` 表后,跨进程 / 跨重启都生效:
- 多 uvicorn worker 都能看到同一份待确认集(不同请求落到不同 worker 也能继续完成);
- 服务重启不丢失用户在浏览器里点开的"待确认"操作;
- TTL 仍为 600 秒,每次 ``get`` 时按 ``created_at`` 校验,过期自动清理。
"""
import json
import uuid
from dataclasses import dataclass, field
from typing import List

from sqlalchemy.orm import Session

from app.models.pending_action import PendingAction
from app.utils import utcnow


@dataclass
class PendingActionView:
    """对调用方暴露的视图,避免让 ORM 对象泄漏到 Agent 业务层。"""

    action_id: str
    tool_calls: List[dict] = field(default_factory=list)
    summary: str = ""
    preview: dict | None = None
    session_id: str = ""
    created_at: float = 0.0


class PendingActionStore:
    """基于 SQLAlchemy ``Session`` 的待确认操作存储。

    旧实现是进程内的 ``dict``,多 worker / 重启即丢。现在 ``create`` /
    ``get`` / ``remove`` 都要求传入 ``Session``,由调用方(API 层)决定事务边界。
    """

    def __init__(self, ttl_seconds: int = 600):
        self.ttl_seconds = ttl_seconds

    def create(
        self,
        db: Session,
        tool_calls: List[dict],
        summary: str,
        preview: dict | None = None,
        session_id: str = "",
    ) -> str:
        action_id = uuid.uuid4().hex[:12]
        record = PendingAction(
            id=action_id,
            session_id=session_id,
            summary=summary,
            tool_calls_json=json.dumps(tool_calls, ensure_ascii=False, default=str),
            preview_json=(
                json.dumps(preview, ensure_ascii=False, default=str)
                if preview
                else None
            ),
            created_at=utcnow(),
        )
        db.add(record)
        db.commit()
        return action_id

    def get(
        self,
        db: Session,
        action_id: str,
        session_id: str = "",
    ) -> PendingActionView | None:
        record = db.get(PendingAction, action_id)
        if record is None:
            return None

        # 会话隔离:另一个 session 不能取走别人的待确认操作。
        if record.session_id and session_id and record.session_id != session_id:
            return None

        # TTL 校验:过期视为不存在,同时清掉记录。
        age = (utcnow() - record.created_at).total_seconds()
        if age >= self.ttl_seconds:
            db.delete(record)
            db.commit()
            return None

        try:
            tool_calls = json.loads(record.tool_calls_json or "[]")
        except json.JSONDecodeError:
            tool_calls = []
        preview = None
        if record.preview_json:
            try:
                preview = json.loads(record.preview_json)
            except json.JSONDecodeError:
                preview = None

        return PendingActionView(
            action_id=record.id,
            tool_calls=tool_calls,
            summary=record.summary,
            preview=preview,
            session_id=record.session_id,
            created_at=record.created_at.timestamp() if record.created_at else 0.0,
        )

    def remove(self, db: Session, action_id: str) -> None:
        record = db.get(PendingAction, action_id)
        if record is None:
            return
        db.delete(record)
        db.commit()
