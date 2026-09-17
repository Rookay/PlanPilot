# -*- coding: UTF-8 -*-
# Project ：mine 
# File    ：task.py
# IDE     ：PyCharm 
# Author  ：刘景涛
# Date    ：2026/9/11 14:10
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

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

    splittable = Column(Boolean, nullable=False, default=False)
    min_chunk_minutes = Column(Integer, nullable=False, default=30)
    max_chunk_minutes = Column(Integer, nullable=False, default=120)

    location_id = Column(
        Integer,
        ForeignKey("saved_locations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    location = Column(String(200), nullable=True)
    location_city = Column(String(100), nullable=True)
    location_longitude = Column(Float, nullable=True)
    location_latitude = Column(Float, nullable=True)

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

    dependency_links = relationship(
        "TaskDependency",
        foreign_keys="TaskDependency.task_id",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def dependency_ids(self):
        return sorted(link.depends_on_task_id for link in self.dependency_links)


class TaskDependency(Base):
    __tablename__ = "task_dependencies"
    __table_args__ = (
        UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_dependency"),
    )

    id = Column(Integer, primary_key=True)
    task_id = Column(
        Integer,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    depends_on_task_id = Column(
        Integer,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
