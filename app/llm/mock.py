# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：mock.py
"""MockProvider：无 Key、无网络的确定性 Provider。

两种用法：
1. 脚本化：构造时传入 ``script``（一系列文本/tool_calls 轮次），用于测试 Agent Loop；
2. 规则兜底：没有脚本时，按简单关键词规则驱动工具，用于无模型演示。
"""

import json
import re
from datetime import timedelta
from typing import Optional

from app.llm.base import LLMProvider, LLMResponse, ToolCall
from app.utils import utcnow


class MockProvider(LLMProvider):
    def __init__(
        self,
        fixed_text: str = "当前使用本地规则模式。",
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
        last_user = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                last_user = message.get("content", "")
                break

        tool_messages = [item for item in messages if item.get("role") == "tool"]
        executed = [item.get("name") for item in tool_messages]
        wants_plan = "安排" in last_user or "规划" in last_user or "排期" in last_user
        wants_undo_plan = any(
            phrase in last_user
            for phrase in ("撤销计划", "撤销排期", "撤销刚才", "恢复上次计划")
        )
        wants_local_replan = any(
            phrase in last_user for phrase in ("局部重排", "重排本周", "重新安排本周")
        )
        urgent = any(word in last_user for word in ("急", "尽快", "优先"))
        wants_weather = any(word in last_user for word in ("天气", "下雨", "下雪", "降温"))
        wants_delete_events = any(word in last_user for word in ("删除", "清空")) and any(
            word in last_user for word in ("事件", "行程", "出去玩", "固定安排")
        )

        if wants_undo_plan and "undo_latest_plan" not in executed:
            return LLMResponse(
                tool_calls=[
                    ToolCall(id="mock_undo_plan", name="undo_latest_plan", arguments={})
                ]
            )

        if wants_local_replan and "replan_local" not in executed:
            start = utcnow().replace(minute=0, second=0, microsecond=0)
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="mock_local_replan",
                        name="replan_local",
                        arguments={
                            "start": start.isoformat() + "Z",
                            "end": (start + timedelta(days=7)).isoformat() + "Z",
                        },
                    )
                ]
            )

        if wants_delete_events:
            if "get_events" not in executed:
                return LLMResponse(
                    tool_calls=[ToolCall(id="mock_get_events", name="get_events", arguments={})]
                )
            if "delete_events" not in executed:
                event_ids = self._matching_event_ids(tool_messages, last_user)
                if event_ids:
                    return LLMResponse(
                        tool_calls=[
                            ToolCall(
                                id="mock_delete_events",
                                name="delete_events",
                                arguments={"event_ids": event_ids},
                            )
                        ]
                    )
                return LLMResponse(text="没有找到名称匹配的固定事件，请再说明要删除的事件名称。")

        if wants_weather and "get_weather" not in executed:
            target = utcnow().date()
            if "明天" in last_user:
                target += timedelta(days=1)
            elif "后天" in last_user:
                target += timedelta(days=2)
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="mock_weather",
                        name="get_weather",
                        arguments={"date": target.isoformat()},
                    )
                ]
            )

        # 离线模式也能完成“找任务 -> 提高优先级 -> 留间隔排期”的多步闭环。
        if wants_plan:
            if urgent and "get_tasks" not in executed:
                keyword = "论文" if "论文" in last_user else ""
                arguments = {"q": keyword} if keyword else {"status": "pending"}
                return LLMResponse(
                    tool_calls=[ToolCall(id="mock_find_urgent", name="get_tasks", arguments=arguments)]
                )

            if urgent and "update_task" not in executed:
                task_id = self._first_task_id(tool_messages)
                if task_id is not None:
                    return LLMResponse(
                        tool_calls=[
                            ToolCall(
                                id="mock_prioritize",
                                name="update_task",
                                arguments={"task_id": task_id, "priority": "high"},
                            )
                        ]
                    )

            if "generate_plan" not in executed:
                gap = 30 if any(word in last_user for word in ("空闲", "间隔", "不要连续")) else None
                arguments = {"days": 7}
                if gap is not None:
                    arguments["gap_minutes"] = gap
                return LLMResponse(
                    tool_calls=[ToolCall(id="mock_plan", name="generate_plan", arguments=arguments)]
                )

        # 工具链完成后，用最近几步的结果作清晰总结。
        if tool_messages:
            summaries = []
            for item in tool_messages[-4:]:
                try:
                    payload = json.loads(item.get("content", "{}"))
                    summary = payload.get("summary")
                    if summary:
                        summaries.append(summary)
                except (json.JSONDecodeError, TypeError):
                    continue
            return LLMResponse(text="已执行完毕：" + "；".join(summaries or ["操作完成"]))

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

    @staticmethod
    def _first_task_id(tool_messages: list[dict]) -> int | None:
        for message in reversed(tool_messages):
            if message.get("name") != "get_tasks":
                continue
            try:
                payload = json.loads(message.get("content", "{}"))
                data = payload.get("data") or []
                if data:
                    return int(data[0]["id"])
            except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                return None
        return None

    @staticmethod
    def _matching_event_ids(tool_messages: list[dict], request: str) -> list[int]:
        for message in reversed(tool_messages):
            if message.get("name") != "get_events":
                continue
            try:
                payload = json.loads(message.get("content", "{}"))
                events = payload.get("data") or []
                delete_all = any(word in request for word in ("全部事件", "所有事件", "清空事件"))
                matched = []
                for event in events:
                    title = str(event.get("title") or "")
                    base_title = re.sub(r"[（(]第?\d+天[）)]", "", title).strip()
                    if delete_all or title in request or (base_title and base_title in request):
                        matched.append(int(event["id"]))
                return matched
            except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                return []
        return []
