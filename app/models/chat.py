# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：chat.py
"""对话消息模型。

role 取值：user（用户）/ assistant（助手）。
"""
from sqlalchemy import Column, DateTime, Integer, String, Text

from app.database import Base
from app.utils import utcnow


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)

    session_id = Column(
        String(64),
        nullable=False,
        default="legacy",
        index=True,
    )

    role = Column(
        String(20),
        nullable=False,
        default="user",
    )

    content = Column(Text, nullable=False)
    agent_run_id = Column(String(32), nullable=True, index=True)

    created_at = Column(
        DateTime,
        default=utcnow,
        nullable=False,
    )
