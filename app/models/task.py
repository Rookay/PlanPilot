# -*- coding: UTF-8 -*-
# Project ：mine 
# File    ：task.py
# IDE     ：PyCharm 
# Author  ：刘景涛
# Date    ：2026/9/11 14:10
from sqlalchemy import Column, DateTime, Integer, String, Text

from app.database import Base
from app.utils import utcnow


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)

    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)

    status = Column(
        String(20),
        nullable=False,
        default="pending",
    )

    priority = Column(
        String(20),
        nullable=False,
        default="medium",
    )

    estimated_duration = Column(
        Integer,
        nullable=False,
        default=60,
    )

    start_time = Column(DateTime, nullable=True)

    deadline = Column(DateTime, nullable=True)
    earliest_start = Column(DateTime, nullable=True)

    preferred_time = Column(
        String(20),
        nullable=True,
    )

    category = Column(
        String(50),
        nullable=True,
    )

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