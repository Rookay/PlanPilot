# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：base.py
"""LLM Provider 抽象。

不绑定任何具体 SDK / 模型。未来可增加 OpenAIProvider / AnthropicProvider 等，
Agent 层只依赖本抽象，从而做到「换模型不影响底层 Task / Calendar / Planning Engine」。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ToolCall:
    """LLM 请求调用的一个工具。"""

    id: str = ""
    name: str = ""
    arguments: dict = field(default_factory=dict)
    #: 模型给出的参数不是合法 JSON 对象时,这里保存面向模型的错误说明。
    #: 非空时 loop 不会执行工具,而是把说明当作 Observation 回传,
    #: 让模型重写整段参数——与"漏填字段"区分开,否则模型只会去补字段。
    parse_error: Optional[str] = None


@dataclass
class LLMResponse:
    """LLM 的一次回复：要么是文本，要么是工具调用请求。"""

    text: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None
    #: 模型返回的原始 assistant 消息（含 reasoning_content 等字段）。
    #: 某些 reasoning 模型（如 DeepSeek）要求后续调用原样回传，否则报 400。
    raw_message: Optional[dict] = None
    usage: dict = field(default_factory=dict)

    def is_text(self) -> bool:
        return self.tool_calls is None


class LLMProvider(ABC):
    """LLM 抽象。所有 Provider 必须实现 chat / structured。"""

    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
    ) -> LLMResponse:
        """多轮对话；tools 为 function-calling schema。"""

    @abstractmethod
    def structured(self, messages: list[dict], output_schema: dict) -> dict:
        """按 JSON Schema 输出结构化结果（用于意图/实体抽取）。"""
