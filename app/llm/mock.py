# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：mock.py
"""MockProvider：无 Key、无网络的确定性 Provider。

用于在没有真实模型的情况下跑通整个 Agent Loop。
后续可通过「脚本化决策」让它在测试里返回预设的 tool_calls 序列。
"""

from typing import Optional

from app.llm.base import LLMProvider, LLMResponse


class MockProvider(LLMProvider):
    def __init__(
        self,
        fixed_text: str = "（MockProvider）模型尚未接入，这是占位回复。",
    ):
        self.fixed_text = fixed_text

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
    ) -> LLMResponse:
        # 第一阶段：始终返回文本，用于验证 Provider 接口与 Agent 骨架可运行。
        # 后续阶段可改为按 messages 内容返回预设 tool_calls。
        return LLMResponse(text=self.fixed_text)

    def structured(self, messages: list[dict], output_schema: dict) -> dict:
        # 第一阶段：返回空对象；接入真实模型后替换为按 output_schema 解析。
        return {}
