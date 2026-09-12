# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：chat.py
from datetime import datetime
from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ChatRole = Literal["user", "assistant"]


class ChatMessageBase(BaseModel):
    role: ChatRole = "user"
    content: str = Field(..., min_length=1, max_length=20000)

    @field_validator("content", mode="before")
    @classmethod
    def _strip_content(cls, value):
        return value.strip() if isinstance(value, str) else value


class ChatMessageCreate(ChatMessageBase):
    pass


class ChatMessageResponse(ChatMessageBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class ChatSendRequest(BaseModel):
    """发送一条用户消息。"""

    content: str = Field(..., min_length=1, max_length=20000)

    @field_validator("content", mode="before")
    @classmethod
    def _strip_content(cls, value):
        return value.strip() if isinstance(value, str) else value


class ChatSendResponse(BaseModel):
    """返回保存后的用户消息和助手回复。"""

    user_message: ChatMessageResponse
    assistant_message: ChatMessageResponse


class ChatHistoryResponse(BaseModel):
    messages: List[ChatMessageResponse]
