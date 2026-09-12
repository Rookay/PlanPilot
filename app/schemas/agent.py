# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：agent.py
"""Agent API 的 Pydantic 模型。"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class AgentChatRequest(BaseModel):
    message: str = Field(default="", max_length=20000)
    session_id: Optional[str] = None
    confirm_action_id: Optional[str] = None
    decision: Optional[Literal["accept", "reject"]] = None

    @model_validator(mode="after")
    def _check_message(self):
        # 确认/拒绝操作可以不带 message；普通对话必须带内容。
        if not self.confirm_action_id and not self.message.strip():
            raise ValueError("message 不能为空")
        return self


class ConfirmationInfoSchema(BaseModel):
    required: bool = False
    action_id: Optional[str] = None
    summary: Optional[str] = None
    changes: List[dict] = Field(default_factory=list)
    preview: Optional[dict] = None


class AgentChatResponse(BaseModel):
    session_id: str
    reply: str
    tool_calls: List[dict] = Field(default_factory=list)
    applied_changes: List[dict] = Field(default_factory=list)
    proposed_schedules: List[dict] = Field(default_factory=list)
    confirmation: ConfirmationInfoSchema = Field(default_factory=ConfirmationInfoSchema)
