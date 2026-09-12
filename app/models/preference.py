# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：preference.py
"""用户偏好（长期记忆的第一版：简单键值存储）。"""
from sqlalchemy import Column, DateTime, String, Text

from app.database import Base
from app.utils import utcnow


class UserPreference(Base):
    __tablename__ = "user_preferences"

    key = Column(String(50), primary_key=True)

    value = Column(Text, nullable=False)

    updated_at = Column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
