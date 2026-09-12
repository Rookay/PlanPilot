# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：mock.py
"""MockProvider：无 Key、无网络的确定性 Provider。

两种用法：
1. 脚本化：构造时传入 ``script``（一系列文本/tool_calls 轮次），用于测试 Agent Loop；
2. 规则兜底：没有脚本时，按简单关键词规则驱动工具，用于无模型演示。
"""

import json
from datetime import timedelta
from typing import Optional

from app.llm.base import LLMProvider, LLMResponse, ToolCall
from app.utils import utcnow


class MockProvider(LLMProvider):
    def __init__(
        self,
        fixed_text: str = "（MockProvider）模型尚未接入，这是占位回复。",
        script: Optional[list] = None,
    ):
        self.fixed_text = fixed_text
        self._script = list(script or [])
        self._index = 0

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
    ) -> LLMResponse:
        if self._index < len(self._script):
            turn = self._script[self._index]
            self._index += 1
            return self._to_response(turn)

        return self._rule_based(messages, tools)

    def structured(self, messages: list[dict], output_schema: dict) -> dict:
        # 无脚本时无法真正抽取，返回空；脚本模式也不做结构化（第一版）。
        return {}

    # ------------------------------------------------------------------

    def _to_response(self, turn: dict) -> LLMResponse:
        if "tool_calls" in turn:
            calls = [
                ToolCall(
                    id=f"mock_{self._index}_{i}",
                    name=call["name"],
                    arguments=call.get("arguments", {}),
                )
                for i, call in enumerate(turn["tool_calls"])
            ]
            return LLMResponse(tool_calls=calls)
        return LLMResponse(text=turn.get("text", self.fixed_text))

    def _rule_based(self, messages: list[dict], tools: Optional[list[dict]]) -> LLMResponse:
        # 工具刚执行完 → 用工具的 summary 收尾。
        if messages and messages[-1].get("role") == "tool":
            summary = "完成"
            try:
                payload = json.loads(messages[-1].get("content", "{}"))
                summary = payload.get("summary") or "完成"
            except (json.JSONDecodeError, TypeError):
                pass
            return LLMResponse(text=f"已执行完毕：{summary}")

        last_user = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                last_user = message.get("content", "")
                break

        if "安排" in last_user or "规划" in last_user:
            return LLMResponse(
                tool_calls=[ToolCall(id="mock_plan", name="generate_plan", arguments={"days": 7})]
            )
        if "冲突" in last_user:
            return LLMResponse(
                tool_calls=[ToolCall(id="mock_conflict", name="check_conflicts", arguments={})]
            )
        if "空闲" in last_user:
            start = (utcnow() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="mock_slots",
                        name="find_free_slots",
                        arguments={"start": start.isoformat() + "Z", "days": 1},
                    )
                ]
            )
        if "任务" in last_user or "待办" in last_user:
            return LLMResponse(
                tool_calls=[ToolCall(id="mock_tasks", name="get_tasks", arguments={})]
            )

        tool_names = [t["function"]["name"] for t in (tools or [])]
        return LLMResponse(
            text=f"（Mock 规则驱动）可用工具：{', '.join(tool_names)}。"
            f"请用自然语言描述需求，例如「帮我安排明天」。"
        )
