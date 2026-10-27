# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：pending_action.py
"""待确认的 Agent 操作集,持久化到数据库。

旧的 ``PendingActionStore`` 用了进程内的 dict,多 worker / 重启即丢;
改成 SQLite 表之后,确认请求可以跨进程、跨重启存活。

表里还带一个领取状态,因为"能跨进程看到同一份待确认集"的另一面是
"两个进程都能对它执行一遍":用户双击确认、或者两个标签页都点了,两份请求
会各自读到同一份 ``tool_calls`` 并各执行一次。所以消费必须是一次性的,
靠条件更新保证(见 ``app/agent/confirmation.py`` 的 ``claim``)。
"""
from sqlalchemy import Column, DateTime, String, Text

from app.database import Base
from app.utils import utcnow

#: 还没被任何一次确认领取。
STATUS_PENDING = "pending"

#: 已被某次确认领取,正在执行工具。
STATUS_TAKEN = "taken"

#: 领取后多久没有落地,视为执行方已崩溃,允许后来者接管。
#: 必须有这个上限:领取状态没有持有者,进程崩在中间时没人会来释放它。
CLAIM_TIMEOUT_SECONDS = 120


class PendingAction(Base):
    __tablename__ = "pending_actions"

    # 与旧实现保持一致:12 字符 hex,方便前端 URL 直接展示。
    id = Column(String(12), primary_key=True)

    session_id = Column(String(64), nullable=False, index=True)

    summary = Column(Text, nullable=False, default="")

    # 工具调用列表,序列化为 JSON;调用时反序列化。
    tool_calls_json = Column(Text, nullable=False, default="[]")

    # 预览数据(可空),同样 JSON 序列化。
    preview_json = Column(Text, nullable=True)

    #: pending / taken。领取用条件更新完成,同一份操作只有一次能成功。
    status = Column(String(16), nullable=False, default=STATUS_PENDING, index=True)

    #: 领取时刻。超过 ``CLAIM_TIMEOUT_SECONDS`` 仍停在 taken,说明执行方没了。
    taken_at = Column(DateTime, nullable=True)

    created_at = Column(
        DateTime,
        default=utcnow,
        nullable=False,
        index=True,
    )
