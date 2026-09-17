# -*- coding: UTF-8 -*-
"""持久化 Agent 的可审计执行轨迹。"""

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text

from app.database import Base
from app.utils import utcnow


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id = Column(String(32), primary_key=True)
    session_id = Column(String(64), nullable=False, index=True)
    message = Column(Text, nullable=True)
    provider = Column(String(100), nullable=True)
    skill = Column(String(100), nullable=True)
    status = Column(String(30), nullable=False, default="completed", index=True)
    duration_ms = Column(Integer, nullable=False, default=0)
    tool_count = Column(Integer, nullable=False, default=0)
    confirmation_required = Column(Boolean, nullable=False, default=False)
    constraint_passed = Column(Boolean, nullable=False, default=True)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    estimated_cost = Column(Float, nullable=False, default=0)
    steps_json = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime, nullable=False, default=utcnow, index=True)
