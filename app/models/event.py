# -*- coding: UTF-8 -*-
# Project ：mine 
# File    ：event.py
# IDE     ：PyCharm 
# Author  ：刘景涛
# Date    ：2026/9/11 14:11
from sqlalchemy import Column, DateTime, Integer, String, Text

from app.database import Base
from app.utils import utcnow


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, index=True)

    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)

    start_time = Column(
        DateTime,
        nullable=False,
    )

    end_time = Column(
        DateTime,
        nullable=False,
    )

    location = Column(
        String(200),
        nullable=True,
    )

    created_at = Column(
        DateTime,
        default=utcnow,
        nullable=False,
    )