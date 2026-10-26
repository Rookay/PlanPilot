# -*- coding: UTF-8 -*-
"""长期事实的输入输出模型。"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

FactStatus = Literal["candidate", "confirmed", "rejected"]


class MemoryFactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    fact: str
    category: str
    status: str
    hit_count: int
    session_id: Optional[str] = None
    created_at: datetime
    last_seen_at: datetime


class FactStatusUpdate(BaseModel):
    status: FactStatus = Field(
        description="confirmed 表示以后每次都注入，rejected 表示不再自动复活"
    )
