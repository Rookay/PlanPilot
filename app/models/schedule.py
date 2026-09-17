# -*- coding: UTF-8 -*-
# Project ：mine 
# File    ：schedule.py
# IDE     ：PyCharm 
# Author  ：刘景涛
# Date    ：2026/9/11 14:11
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base
from app.utils import utcnow


class Schedule(Base):
    __tablename__ = "schedules"

    id = Column(Integer, primary_key=True, index=True)

    task_id = Column(
        Integer,
        ForeignKey("tasks.id"),
        nullable=False,
    )

    start_time = Column(
        DateTime,
        nullable=False,
    )

    end_time = Column(
        DateTime,
        nullable=False,
    )

    status = Column(
        String(20),
        nullable=False,
        default="proposed",
    )

    plan_batch_id = Column(String(32), nullable=True, index=True)
    reason = Column(Text, nullable=True)

    created_at = Column(
        DateTime,
        default=utcnow,
        nullable=False,
    )

    updated_at = Column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
