# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：confirmation.py
"""Confirmation:待确认变更集的持久化存储与提交。

落库到 ``pending_actions`` 表后,跨进程 / 跨重启都生效:
- 多 uvicorn worker 都能看到同一份待确认集(不同请求落到不同 worker 也能继续完成);
- 服务重启不丢失用户在浏览器里点开的"待确认"操作;
- TTL 仍为 600 秒,每次读取时按 ``created_at`` 校验,过期自动清理。

**消费是一次性的。** "能跨进程看到同一份"的另一面就是"两个进程都能执行一遍":
用户双击确认、或者两个标签页都点了,两份请求会读到同一份 ``tool_calls`` 各跑
一次。这里不用锁,因为锁的三个前提都不成立:

- 进程内的 ``threading.Lock`` 只在同一进程内有效,多 worker 各自一把,互相看不见;
- 数据库锁(``SELECT ... FOR UPDATE``、advisory lock)要持有到事务结束,等于把
  秒级的工具执行圈进临界区,所有写请求排队;
- 锁有持有者、有释放动作;领取状态没有持有者,进程崩在中间时没人来释放。

所以改用**条件更新**:``UPDATE ... WHERE id=? AND status='pending'``,只有一条
语句能改到行,``rowcount`` 就是"抢到没有"。领取后立刻提交,执行期间不持有任何
东西;失败退回 pending 供重试,崩溃留下的 taken 由 ``CLAIM_TIMEOUT_SECONDS``
超时接管(相当于租约)。
"""
import json
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.pending_action import (
    CLAIM_TIMEOUT_SECONDS,
    STATUS_PENDING,
    STATUS_TAKEN,
    PendingAction,
)
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


def _view(record: PendingAction) -> PendingActionView:
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


class PendingActionStore:
    """基于 SQLAlchemy ``Session`` 的待确认操作存储。

    旧实现是进程内的 ``dict``,多 worker / 重启即丢。现在所有方法都要求传入
    ``Session``,由调用方(API 层)决定事务边界。
    """

    def __init__(
        self,
        ttl_seconds: int = 600,
        claim_timeout_seconds: int = CLAIM_TIMEOUT_SECONDS,
    ):
        self.ttl_seconds = ttl_seconds
        self.claim_timeout_seconds = claim_timeout_seconds

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
            status=STATUS_PENDING,
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
    ) -> Optional[PendingActionView]:
        """只读预览,不改变领取状态。前端展示"待确认"时用这个。"""

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

        return _view(record)

    def claim(
        self,
        db: Session,
        action_id: str,
        session_id: str = "",
    ) -> Optional[PendingActionView]:
        """抢占待确认操作。返回 ``None`` 表示没抢到(不存在/过期/已被处理)。

        抢占成功后状态变为 ``taken`` 并**立即提交**:其它进程必须能马上看见,
        否则两个请求会各自拿着未提交的 taken 状态各跑一遍,退化成没有抢占。
        """

        record = db.get(PendingAction, action_id)
        if record is None:
            return None

        # 会话隔离要在抢占之前判定,否则会把别人的待确认操作标成 taken。
        if record.session_id and session_id and record.session_id != session_id:
            return None

        age = (utcnow() - record.created_at).total_seconds()
        if age >= self.ttl_seconds:
            db.delete(record)
            db.commit()
            return None

        now = utcnow()
        cutoff = now - timedelta(seconds=self.claim_timeout_seconds)
        # 判断条件写进 WHERE,由数据库保证"只有一个请求能改到这一行"。
        # 注释掉的等价写法:先 SELECT 判断再改,中间就是竞态窗口。
        claimable = or_(
            PendingAction.status == STATUS_PENDING,
            and_(
                PendingAction.status == STATUS_TAKEN,
                or_(
                    PendingAction.taken_at.is_(None),
                    PendingAction.taken_at < cutoff,
                ),
            ),
        )
        claimed = (
            db.query(PendingAction)
            .filter(PendingAction.id == action_id, claimable)
            .update(
                {"status": STATUS_TAKEN, "taken_at": now},
                synchronize_session=False,
            )
        )
        db.commit()

        if not claimed:
            # 另一个请求已经抢走;或状态是 taken 且还没到超时接管时间。
            return None

        # commit 已让 identity map 过期,这里读到的就是提交后的状态。
        return _view(record)

    def release(self, db: Session, action_id: str) -> None:
        """把领取退回 pending。执行失败时调用,让用户可以重试。"""

        db.query(PendingAction).filter(PendingAction.id == action_id).update(
            {"status": STATUS_PENDING, "taken_at": None},
            synchronize_session=False,
        )
        db.commit()

    def remove(self, db: Session, action_id: str) -> None:
        """执行成功后删除。已被别人删掉时不报错。"""

        record = db.get(PendingAction, action_id)
        if record is None:
            return
        db.delete(record)
        db.commit()
