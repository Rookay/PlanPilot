# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：chat.py
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

ChatRole = Literal["user", "assistant"]


class ChatMessageBase(BaseModel):
    role: ChatRole = "user"
    content: str = Field(..., min_length=1, max_length=20000)

    @field_validator("content", mode="before")
    @classmethod
    def _strip_content(cls, value):
        return value.strip() if isinstance(value, str) else value


class ChatMessageResponse(ChatMessageBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: str = "legacy"
    agent_run_id: Optional[str] = None
    created_at: datetime
