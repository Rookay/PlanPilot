# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：pending_action.py
"""待确认的 Agent 操作集,持久化到数据库。

旧的 ``PendingActionStore`` 用了进程内的 dict,多 worker / 重启即丢;
改成 SQLite 表之后,确认请求可以跨进程、跨重启存活。
"""
from sqlalchemy import Column, DateTime, String, Text

from app.database import Base
from app.utils import utcnow


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

    created_at = Column(
        DateTime,
        default=utcnow,
        nullable=False,
        index=True,
    )