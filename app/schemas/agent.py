# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：agent.py
"""Agent API 的 Pydantic 模型。

输入校验集中在 :class:`AgentChatRequest` 上：

- ``message``: ``max_length=20000`` 是为了挡住无穷输入吃 token / 钱。普通对话
  的实际 prompt token 由 history + system + world snapshot + skill 决定，单条
  message 设到这个量级已经接近单轮 8k 上下文，行为已经在退化边缘。
- ``session_id``: ``[A-Za-z0-9_-]+`` 限制防止 SQL/路径注入或日记垃圾。
- ``confirm_action_id``: 32 hex，定长留余。
- ``decision``: enum，限制在 ``accept`` / ``reject``，免得未来加新值漏处理。
- ``_check_message``: 业务级校验：普通对话必须带内容，确认操作必须带 decision。
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class AgentChatRequest(BaseModel):
    message: str = Field(
        default="",
        max_length=20000,
        description="用户消息；上限 20000 字符约为 7k token，避免单请求压爆上下文。",
    )
    session_id: Optional[str] = Field(
        default=None,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="会话 id；留空自动生成。字符限制防止注入或日志污染。",
    )
    confirm_action_id: Optional[str] = Field(
        default=None,
        max_length=32,
        description="待确认操作 id；PendingActionStore 用的 12-hex，留余。",
    )
    decision: Optional[Literal["accept", "reject"]] = Field(
        default=None,
        description="用户对 confirmation 的决定。",
    )
    request_id: Optional[str] = Field(
        default=None,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description=(
            "幂等键，同一逻辑请求重发时必须携带同一个值。"
            "留空则每次都当作新请求，不去重（兼容旧客户端）。"
        ),
    )

    @model_validator(mode="after")
    def _check_message(self):
        # 确认/拒绝操作可以不带 message；普通对话必须带内容。
        if not self.confirm_action_id and not self.message.strip():
            raise ValueError("message 不能为空")
        if self.confirm_action_id and self.decision is None:
            raise ValueError("确认操作必须提供 decision")
        if self.decision is not None and not self.confirm_action_id:
            raise ValueError("decision 必须与 confirm_action_id 一起提供")
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
    workflow_steps: List[dict] = Field(default_factory=list)
    run_id: Optional[str] = None
    duration_ms: int = 0
    run_status: str = "completed"
    model_usage: dict = Field(default_factory=dict)
    estimated_cost: float = 0
