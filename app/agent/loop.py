# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：loop.py
"""Agent Loop：LLM 决策 + 工具执行 + 结果观察 的循环。"""

import json
import time
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.orm import Session

from app.agent.confirmation import PendingActionStore
from app.agent.context import build_system_prompt, build_world_summary
from app.agent.memory import Memory
from app.llm.base import LLMProvider
from app.llm.mock import MockProvider
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
    workflow_steps: List[dict] = field(default_factory=list)


class AgentLoop:
    MAX_ITERATIONS = 12
    MAX_TOOL_CALLS = 24
    FALLBACK_COOLDOWN_SECONDS = 60

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
        self.fallback_provider = MockProvider()
        self.fallback_until = 0.0
        self.last_provider_error: Optional[str] = None

    def run(self, db: Session, message: str, session_id: str) -> AgentResult:
        self.memory.add(session_id, "user", message)

        messages = self._build_messages(db, session_id, message)

        tool_call_log: List[dict] = []
        applied_changes: List[dict] = []
        proposed_schedules: List[dict] = []
        tool_call_count = 0
        seen_calls: set[str] = set()

        for _ in range(self.MAX_ITERATIONS):
            try:
                provider = (
                    self.fallback_provider
                    if time.monotonic() < self.fallback_until
                    else self.llm
                )
                response = provider.chat(messages, tools=self.registry.schemas())
            except Exception as exc:  # noqa: BLE001
                self.last_provider_error = str(exc)
                self.fallback_until = time.monotonic() + self.FALLBACK_COOLDOWN_SECONDS
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "真实模型暂时不可用，已切换到本地规则模式。"
                            f"错误摘要：{exc}"
                        ),
                    }
                )
                response = self.fallback_provider.chat(
                    messages, tools=self.registry.schemas()
                )

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

            pending_confirm_calls: List[dict] = []
            pending_summaries: List[str] = []
            pending_previews: List[dict] = []

            for call in response.tool_calls:
                tool = self.registry.get(call.name)
                call_key = self._call_key(call.name, call.arguments)

                if tool is None:
                    messages.append(self._tool_message(call.id, call.name, False, f"unknown tool: {call.name}"))
                    continue

                if call_key in seen_calls:
                    reply = self._progress_reply(tool_call_log, proposed_schedules)
                    self.memory.add(session_id, "assistant", reply)
                    return AgentResult(
                        reply=reply,
                        session_id=session_id,
                        tool_calls=tool_call_log,
                        applied_changes=applied_changes,
                        proposed_schedules=proposed_schedules,
                    )
                seen_calls.add(call_key)

                # confirm 级别：收集同一轮的全部变更，稍后合并成一次确认。
                if tool.confirmation == "confirm":
                    preview = self.registry.preview(call.name, call.arguments, db)
                    if not preview.success:
                        messages.append(
                            self._tool_message(
                                call.id,
                                call.name,
                                False,
                                error=preview.error or "无法生成操作预览",
                            )
                        )
                        tool_call_log.append(
                            {
                                "name": call.name,
                                "arguments": call.arguments,
                                "success": False,
                                "summary": preview.error,
                            }
                        )
                        continue
                    pending_confirm_calls.append(
                        {"name": call.name, "args": call.arguments}
                    )
                    pending_summaries.append(
                        preview.summary or f"即将执行 {call.name}"
                    )
                    if preview.data is not None:
                        pending_previews.append(preview.data)
                    continue

                # 执行并观察结果。
                result = self.registry.execute(call.name, call.arguments, db)
                tool_call_count += 1

                tool_call_log.append(
                    {
                        "stage": self._tool_stage(call.name),
                        "name": call.name,
                        "arguments": call.arguments,
                        "success": result.success,
                        "summary": result.summary,
                    }
                )

                if result.success and tool.confirmation != "none":
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

            if pending_confirm_calls:
                if len(pending_summaries) == 1:
                    confirmation_summary = pending_summaries[0]
                else:
                    confirmation_summary = (
                        f"将执行 {len(pending_summaries)} 项操作："
                        + "；".join(pending_summaries)
                    )
                combined_preview = (
                    pending_previews[0]
                    if len(pending_previews) == 1
                    else {"items": pending_previews}
                )
                action_id = self.confirmation_store.create(
                    tool_calls=pending_confirm_calls,
                    summary=confirmation_summary,
                    preview=combined_preview,
                    session_id=session_id,
                )
                confirmation_reply = f"需要你确认：{confirmation_summary}"
                self.memory.add(session_id, "assistant", confirmation_reply)
                return AgentResult(
                    reply=confirmation_reply,
                    session_id=session_id,
                    tool_calls=tool_call_log,
                    applied_changes=applied_changes,
                    proposed_schedules=proposed_schedules,
                    confirmation=ConfirmationInfo(
                        required=True,
                        action_id=action_id,
                        summary=confirmation_summary,
                        changes=[
                            {"name": item["name"], "arguments": item["args"]}
                            for item in pending_confirm_calls
                        ],
                        preview=combined_preview,
                    ),
                )

            if tool_call_count >= self.MAX_TOOL_CALLS:
                reply = self._progress_reply(tool_call_log, proposed_schedules)
                self.memory.add(session_id, "assistant", reply)
                return AgentResult(
                    reply=reply,
                    session_id=session_id,
                    tool_calls=tool_call_log,
                    applied_changes=applied_changes,
                    proposed_schedules=proposed_schedules,
                )

        reply = self._progress_reply(tool_call_log, proposed_schedules)
        self.memory.add(session_id, "assistant", reply)
        return AgentResult(
            reply=reply,
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
    def _call_key(name: str, arguments: dict) -> str:
        return json.dumps(
            {"name": name, "arguments": arguments or {}},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    @staticmethod
    def _tool_stage(name: str) -> str:
        if name.startswith("get_") or name in {"find_free_slots"}:
            return "读取"
        if name in {"evaluate_plan", "check_conflicts", "estimate_travel_time"}:
            return "验证"
        if name in {"generate_plan", "replan", "replan_local"}:
            return "规划"
        return "执行"

    @staticmethod
    def _progress_reply(tool_call_log: List[dict], proposed_schedules: List[dict]) -> str:
        if not tool_call_log:
            return "我还没能完成这次处理。可以换一句更具体的话再试，比如「把写论文设为高优先级，并重新规划未来 7 天，任务间隔 30 分钟」。"

        summaries = [
            item.get("summary")
            for item in tool_call_log
            if item.get("success") and item.get("summary")
        ]
        if proposed_schedules:
            return (
                "我已经执行了相关操作，并生成了新的待确认日程。"
                f"本次共生成 {len(proposed_schedules)} 条日程；"
                "你可以到「日程规划」或「日历」里查看并确认。"
            )
        if summaries:
            return "我已经执行到这里：" + "；".join(summaries[-4:])
        return "我尝试处理了这次请求，但工具没有返回可用结果，请稍微换一种说法再试。"

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
