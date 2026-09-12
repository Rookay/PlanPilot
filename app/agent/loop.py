# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：loop.py
"""Agent Loop：LLM 决策 + 工具执行 + 结果观察 的循环。"""

import json
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.orm import Session

from app.agent.confirmation import PendingActionStore
from app.agent.context import build_system_prompt, build_world_summary
from app.agent.memory import Memory
from app.llm.base import LLMProvider
from app.skills.base import SkillRegistry
from app.tools.registry import ToolRegistry


@dataclass
class ConfirmationInfo:
    required: bool = False
    action_id: Optional[str] = None
    summary: Optional[str] = None
    changes: List[dict] = field(default_factory=list)
    preview: Optional[dict] = None


@dataclass
class AgentResult:
    reply: str
    session_id: str = ""
    tool_calls: List[dict] = field(default_factory=list)
    applied_changes: List[dict] = field(default_factory=list)
    proposed_schedules: List[dict] = field(default_factory=list)
    confirmation: ConfirmationInfo = field(default_factory=ConfirmationInfo)


class AgentLoop:
    MAX_ITERATIONS = 8
    MAX_TOOL_CALLS = 16

    def __init__(
        self,
        llm: LLMProvider,
        registry: ToolRegistry,
        memory: Memory,
        confirmation_store: PendingActionStore,
        skill_registry: Optional[SkillRegistry] = None,
    ):
        self.llm = llm
        self.registry = registry
        self.memory = memory
        self.confirmation_store = confirmation_store
        self.skill_registry = skill_registry

    def run(self, db: Session, message: str, session_id: str) -> AgentResult:
        self.memory.add(session_id, "user", message)

        messages = self._build_messages(db, session_id, message)

        tool_call_log: List[dict] = []
        applied_changes: List[dict] = []
        proposed_schedules: List[dict] = []
        tool_call_count = 0

        for _ in range(self.MAX_ITERATIONS):
            response = self.llm.chat(messages, tools=self.registry.schemas())

            if response.is_text() or not response.tool_calls:
                reply = response.text or "（模型未返回内容）"
                self.memory.add_message(
                    session_id,
                    response.raw_message or {"role": "assistant", "content": reply},
                )
                return AgentResult(
                    reply=reply,
                    session_id=session_id,
                    tool_calls=tool_call_log,
                    applied_changes=applied_changes,
                    proposed_schedules=proposed_schedules,
                )

            # 先记录本轮 assistant 消息（原样回传 reasoning_content 等字段）。
            messages.append(self._assistant_message(response))

            for call in response.tool_calls:
                tool = self.registry.get(call.name)

                if tool is None:
                    messages.append(self._tool_message(call.id, call.name, False, f"unknown tool: {call.name}"))
                    continue

                # confirm 级别：先暂停询问用户。
                if tool.confirmation == "confirm":
                    preview = self.registry.preview(call.name, call.arguments, db)
                    action_id = self.confirmation_store.create(
                        tool_calls=[{"name": call.name, "args": call.arguments}],
                        summary=preview.summary or f"即将执行 {call.name}",
                        preview=preview.data,
                    )
                    return AgentResult(
                        reply=f"需要你确认：{preview.summary}",
                        session_id=session_id,
                        tool_calls=tool_call_log,
                        applied_changes=applied_changes,
                        proposed_schedules=proposed_schedules,
                        confirmation=ConfirmationInfo(
                            required=True,
                            action_id=action_id,
                            summary=preview.summary,
                            changes=[{"name": call.name, "arguments": call.arguments}],
                            preview=preview.data,
                        ),
                    )

                # 执行并观察结果。
                result = self.registry.execute(call.name, call.arguments, db)
                tool_call_count += 1

                tool_call_log.append(
                    {
                        "name": call.name,
                        "arguments": call.arguments,
                        "success": result.success,
                        "summary": result.summary,
                    }
                )

                if result.success:
                    applied_changes.append(
                        {"name": call.name, "summary": result.summary}
                    )
                    if (
                        call.name in ("generate_plan", "replan")
                        and isinstance(result.data, dict)
                        and isinstance(result.data.get("schedules"), list)
                    ):
                        proposed_schedules = result.data["schedules"]

                messages.append(
                    self._tool_message(
                        call.id,
                        call.name,
                        result.success,
                        result.summary,
                        result.data,
                        result.error,
                    )
                )

            if tool_call_count >= self.MAX_TOOL_CALLS:
                return AgentResult(
                    reply="已达到最大工具调用次数，请更具体地描述需求。",
                    session_id=session_id,
                    tool_calls=tool_call_log,
                    applied_changes=applied_changes,
                    proposed_schedules=proposed_schedules,
                )

        return AgentResult(
            reply="达到最大推理步数，请重新描述需求。",
            session_id=session_id,
            tool_calls=tool_call_log,
            applied_changes=applied_changes,
            proposed_schedules=proposed_schedules,
        )

    # ------------------------------------------------------------------

    def _build_messages(self, db: Session, session_id: str, message: str) -> List[dict]:
        timezone = self.memory.get_preferences(db).get("timezone", "Asia/Shanghai")

        system = build_system_prompt(timezone)

        if self.skill_registry is not None:
            skill = self.skill_registry.select(message)
            if skill is not None:
                system += "\n\n" + skill.system_hint

        system += "\n\n当前数据快照：\n" + build_world_summary(db, timezone)

        messages: List[dict] = [{"role": "system", "content": system}]
        messages.extend(self.memory.recent(session_id))
        return messages

    def _assistant_message(self, response) -> dict:
        if response.raw_message is not None:
            return response.raw_message
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in (response.tool_calls or [])
            ],
        }

    @staticmethod
    def _tool_message(tool_call_id, name, success, summary=None, data=None, error=None) -> dict:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": json.dumps(
                {
                    "success": success,
                    "summary": summary,
                    "data": data,
                    "error": error,
                },
                ensure_ascii=False,
                default=str,
            ),
        }
