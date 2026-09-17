# -*- coding: UTF-8 -*-
"""用户保存的常用地点。"""

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String

from app.database import Base
from app.utils import utcnow


class SavedLocation(Base):
    __tablename__ = "saved_locations"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String(50), nullable=False, unique=True, index=True)
    address = Column(String(200), nullable=True)
    city = Column(String(100), nullable=True)
    longitude = Column(Float, nullable=False)
    latitude = Column(Float, nullable=False)
    is_default = Column(Boolean, nullable=False, default=False, index=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
