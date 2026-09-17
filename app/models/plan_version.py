# -*- coding: UTF-8 -*-
"""计划版本：记录日程变更前快照及可读差异。"""

from sqlalchemy import Column, DateTime, String, Text

from app.database import Base
from app.utils import utcnow


class PlanVersion(Base):
    __tablename__ = "plan_versions"

    id = Column(String(32), primary_key=True)
    action = Column(String(40), nullable=False)
    summary = Column(String(300), nullable=False)
    status = Column(String(20), nullable=False, default="active", index=True)
    before_snapshot = Column(Text, nullable=False)
    changes = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime, nullable=False, default=utcnow, index=True)
    undone_at = Column(DateTime, nullable=True)
