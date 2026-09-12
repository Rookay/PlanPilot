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

    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class LLMResponse:
    """LLM 的一次回复：要么是文本，要么是工具调用请求。"""

    text: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None

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
