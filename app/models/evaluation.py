# -*- coding: UTF-8 -*-
"""Agent 回归评测运行记录。"""

from sqlalchemy import Column, DateTime, Float, Integer, String, Text

from app.database import Base
from app.utils import utcnow


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"

    id = Column(String(32), primary_key=True)
    mode = Column(String(30), nullable=False, default="deterministic")
    score = Column(Float, nullable=False, default=0)
    scenario_count = Column(Integer, nullable=False, default=0)
    passed_count = Column(Integer, nullable=False, default=0)
    metrics_json = Column(Text, nullable=False, default="{}")
    results_json = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime, nullable=False, default=utcnow, index=True)
