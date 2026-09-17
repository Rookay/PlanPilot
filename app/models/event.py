# -*- coding: UTF-8 -*-
# Project ：mine 
# File    ：event.py
# IDE     ：PyCharm 
# Author  ：刘景涛
# Date    ：2026/9/11 14:11
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text

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

    event_type = Column(
        String(20),
        nullable=False,
        default="activity",
    )
    travel_mode = Column(String(20), nullable=True)
    trip_id = Column(String(32), nullable=True, index=True)
    trip_title = Column(String(200), nullable=True)
    trip_order = Column(Integer, nullable=True)

    location = Column(
        String(200),
        nullable=True,
    )

    location_id = Column(
        Integer,
        ForeignKey("saved_locations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    location_city = Column(String(100), nullable=True)
    location_longitude = Column(Float, nullable=True)
    location_latitude = Column(Float, nullable=True)

    origin_location = Column(String(200), nullable=True)
    origin_location_id = Column(
        Integer,
        ForeignKey("saved_locations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    origin_location_city = Column(String(100), nullable=True)
    origin_location_longitude = Column(Float, nullable=True)
    origin_location_latitude = Column(Float, nullable=True)

    created_at = Column(
        DateTime,
        default=utcnow,
        nullable=False,
    )
