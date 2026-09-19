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
    run_id: Optional[str] = None
    duration_ms: int = 0
    run_status: str = "completed"
    model_usage: dict = field(default_factory=dict)


class AgentLoop:
    MAX_ITERATIONS = 12
    MAX_TOOL_CALLS = 24
    HISTORY_MESSAGE_LIMIT = 10
    HISTORY_CONTENT_LIMIT = 1800
    TOOL_CALL_LIMITS = {
        "get_weather": 3,
        "get_events": 3,
        "get_tasks": 3,
        "get_schedules": 3,
        "generate_plan": 2,
        "replan": 2,
        "replan_local": 2,
        "check_conflicts": 1,
        "evaluate_plan": 1,
        "search_places": 8,
        "geocode_address": 8,
        "estimate_travel_time": 12,
    }
    FALLBACK_COOLDOWN_SECONDS = 15

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
        self.last_run_provider: Optional[str] = None

    def run(self, db: Session, message: str, session_id: str) -> AgentResult:
        self.memory.add(session_id, "user", message)

        selected_skill = (
            self.skill_registry.select(message)
            if self.skill_registry is not None
            else None
        )
        active_tool_names = (
            set(selected_skill.allowed_tools)
            if selected_skill is not None and selected_skill.allowed_tools is not None
            else None
        )
        messages = self._build_messages(db, session_id, message, selected_skill)

        tool_call_log: List[dict] = []
        applied_changes: List[dict] = []
        proposed_schedules: List[dict] = []
        tool_call_count = 0
        seen_calls: set[str] = set()
        tool_call_counts: dict[str, int] = {}
        saturated_tools: set[str] = set()
        model_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        provider = self.llm

        for _ in range(self.MAX_ITERATIONS):
            try:
                provider = (
                    self.fallback_provider
                    if time.monotonic() < self.fallback_until
                    else self.llm
                )
                self.last_run_provider = provider.__class__.__name__
                response = provider.chat(
                    messages,
                    tools=self._available_tool_schemas(
                        saturated_tools,
                        active_tool_names,
                    ),
                )
                if provider is self.llm:
                    self.mark_provider_available()
            except Exception as exc:  # noqa: BLE001
                self.mark_provider_unavailable(exc)
                self.last_run_provider = self.fallback_provider.__class__.__name__
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
                    messages,
                    tools=self._available_tool_schemas(
                        saturated_tools,
                        active_tool_names,
                    ),
                )

            self._merge_usage(model_usage, response.usage)

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
                    model_usage=model_usage,
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
                    tool_call_log.append(
                        {
                            "stage": "执行",
                            "name": call.name,
                            "success": False,
                            "summary": f"未知工具：{call.name}",
                            "duration_ms": 0,
                        }
                    )
                    continue

                if call_key in seen_calls:
                    saturated_tools.add(call.name)
                    error = "同一工具参数已经执行过，请根据已有结果直接作答"
                    messages.append(
                        self._tool_message(call.id, call.name, False, error=error)
                    )
                    tool_call_log.append(
                        {
                            "stage": self._tool_stage(call.name),
                            "name": call.name,
                            "arguments": call.arguments,
                            "success": False,
                            "limited": True,
                            "summary": error,
                            "duration_ms": 0,
                        }
                    )
                    continue
                seen_calls.add(call_key)

                call_limit = self.TOOL_CALL_LIMITS.get(call.name)
                previous_calls = tool_call_counts.get(call.name, 0)
                if call_limit is not None and previous_calls >= call_limit:
                    saturated_tools.add(call.name)
                    error = (
                        f"{call.name} 已达到本轮 {call_limit} 次调用上限；"
                        "请使用已有结果完成回答"
                    )
                    messages.append(
                        self._tool_message(call.id, call.name, False, error=error)
                    )
                    tool_call_log.append(
                        {
                            "stage": self._tool_stage(call.name),
                            "name": call.name,
                            "arguments": call.arguments,
                            "success": False,
                            "limited": True,
                            "summary": error,
                            "duration_ms": 0,
                        }
                    )
                    continue
                tool_call_counts[call.name] = previous_calls + 1
                if call_limit is not None and previous_calls + 1 >= call_limit:
                    saturated_tools.add(call.name)

                # confirm 级别：收集同一轮的全部变更，稍后合并成一次确认。
                if tool.confirmation == "confirm":
                    preview_started = time.perf_counter()
                    preview = self.registry.preview(call.name, call.arguments, db)
                    preview_duration = int(
                        (time.perf_counter() - preview_started) * 1000
                    )
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
                                "duration_ms": preview_duration,
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
                    tool_call_log.append(
                        {
                            "stage": self._tool_stage(call.name),
                            "name": call.name,
                            "arguments": call.arguments,
                            "success": True,
                            "summary": preview.summary,
                            "duration_ms": preview_duration,
                            "awaiting_confirmation": True,
                        }
                    )
                    continue

                # 执行并观察结果。
                execute_started = time.perf_counter()
                result = self.registry.execute(call.name, call.arguments, db)
                execute_duration = int(
                    (time.perf_counter() - execute_started) * 1000
                )
                tool_call_count += 1

                tool_call_log.append(
                    {
                        "stage": self._tool_stage(call.name),
                        "name": call.name,
                        "arguments": call.arguments,
                        "success": result.success,
                        "summary": result.summary,
                        "duration_ms": execute_duration,
                    }
                )

                if result.success and tool.confirmation != "none":
                    applied_changes.append(
                        {
                            "name": call.name,
                            "summary": result.summary,
                            "data": result.data,
                        }
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
                    model_usage=model_usage,
                )

            if tool_call_count >= self.MAX_TOOL_CALLS:
                reply = self._finalize_without_tools(
                    provider,
                    messages,
                    tool_call_log,
                    proposed_schedules,
                    model_usage,
                )
                self.memory.add(session_id, "assistant", reply)
                return AgentResult(
                    reply=reply,
                    session_id=session_id,
                    tool_calls=tool_call_log,
                    applied_changes=applied_changes,
                    proposed_schedules=proposed_schedules,
                    model_usage=model_usage,
                )

        reply = self._finalize_without_tools(
            provider,
            messages,
            tool_call_log,
            proposed_schedules,
            model_usage,
        )
        self.memory.add(session_id, "assistant", reply)
        return AgentResult(
            reply=reply,
            session_id=session_id,
            tool_calls=tool_call_log,
            applied_changes=applied_changes,
            proposed_schedules=proposed_schedules,
            model_usage=model_usage,
        )

    def replace_provider(self, provider: LLMProvider) -> None:
        self.llm = provider
        self.mark_provider_available()

    def mark_provider_available(self) -> None:
        self.fallback_until = 0.0
        self.last_provider_error = None

    def mark_provider_unavailable(self, error: Exception | str) -> None:
        self.last_provider_error = str(error)
        self.fallback_until = time.monotonic() + self.FALLBACK_COOLDOWN_SECONDS

    def retry_after_seconds(self) -> int:
        return max(0, int(self.fallback_until - time.monotonic() + 0.999))

    # ------------------------------------------------------------------

    def _build_messages(
        self,
        db: Session,
        session_id: str,
        message: str,
        selected_skill=None,
    ) -> List[dict]:
        timezone = self.memory.get_preferences(db).get("timezone", "Asia/Shanghai")

        system = build_system_prompt(timezone)

        if selected_skill is None and self.skill_registry is not None:
            selected_skill = self.skill_registry.select(message)
        if selected_skill is not None:
            system += "\n\n" + selected_skill.system_hint

        system += "\n\n当前数据快照：\n" + build_world_summary(db, timezone)

        messages: List[dict] = [{"role": "system", "content": system}]
        messages.extend(
            self._compact_history(
                self.memory.recent(session_id, limit=self.HISTORY_MESSAGE_LIMIT)
            )
        )
        return messages

    def _available_tool_schemas(
        self,
        saturated_tools: set[str],
        active_tool_names: Optional[set[str]] = None,
    ) -> List[dict]:
        return [
            schema
            for schema in self.registry.schemas()
            if schema.get("function", {}).get("name") not in saturated_tools
            and (
                active_tool_names is None
                or schema.get("function", {}).get("name") in active_tool_names
            )
        ]

    def _compact_history(self, messages: List[dict]) -> List[dict]:
        compacted = []
        for message in messages:
            item = dict(message)
            content = item.get("content")
            if isinstance(content, str) and len(content) > self.HISTORY_CONTENT_LIMIT:
                tail_length = min(300, self.HISTORY_CONTENT_LIMIT // 4)
                head_length = self.HISTORY_CONTENT_LIMIT - tail_length
                item["content"] = (
                    content[:head_length]
                    + "\n...[较长历史内容已压缩]...\n"
                    + content[-tail_length:]
                )
            compacted.append(item)
        return compacted

    def _finalize_without_tools(
        self,
        provider,
        messages: List[dict],
        tool_call_log: List[dict],
        proposed_schedules: List[dict],
        model_usage: dict,
    ) -> str:
        final_messages = messages + [
            {
                "role": "system",
                "content": (
                    "工具调用阶段已经结束。禁止继续调用任何工具；请仅根据已有工具结果，"
                    "用简体中文给出简洁结论。若信息不完整，明确指出缺失项并给出当前可判断的结果。"
                ),
            }
        ]
        try:
            response = provider.chat(final_messages, tools=None)
            self._merge_usage(model_usage, response.usage)
            if response.is_text() and response.text:
                return response.text
        except Exception:  # noqa: BLE001 - 最终总结失败时仍返回确定性结果
            pass
        return self._progress_reply(tool_call_log, proposed_schedules)

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
        if name in {
            "generate_plan",
            "replan",
            "replan_local",
            "create_events",
            "revise_trip",
        }:
            return "规划"
        return "执行"

    @staticmethod
    def tool_label(name: str) -> str:
        labels = {
            "get_tasks": "读取任务",
            "get_events": "读取固定行程",
            "get_trips": "定位行程组",
            "search_places": "搜索地点",
            "geocode_address": "解析地点",
            "estimate_travel_time": "验证路线时间",
            "create_events": "生成完整行程",
            "revise_trip": "整体调整行程",
            "generate_plan": "生成任务安排",
            "replan": "重新规划",
            "replan_local": "局部重新规划",
            "check_conflicts": "检查冲突",
            "evaluate_plan": "评估方案",
            "update_task": "更新任务",
            "update_event": "更新事件",
            "delete_events": "批量删除事件",
            "postflight_audit": "执行后复查",
        }
        return labels.get(name, name.replace("_", " "))

    @staticmethod
    def _merge_usage(total: dict, usage: dict | None) -> None:
        usage = usage or {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            try:
                total[key] += max(0, int(usage.get(key) or 0))
            except (TypeError, ValueError):
                continue

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
                    "data": _compact_tool_payload(data),
                    "error": error,
                },
                ensure_ascii=False,
                default=str,
            ),
        }


def _compact_tool_payload(value, key: str = "", depth: int = 0):
    """Keep decision-relevant tool output while dropping token-heavy detail."""

    if depth >= 7:
        return "[嵌套内容已省略]"
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            item_key: _compact_tool_payload(item_value, item_key, depth + 1)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        if key == "path":
            return {
                "point_count": len(value),
                "start": _compact_tool_payload(value[0], depth=depth + 1) if value else None,
                "end": _compact_tool_payload(value[-1], depth=depth + 1) if value else None,
            }
        limit = 8 if key == "steps" else 30
        items = [
            _compact_tool_payload(item, depth=depth + 1)
            for item in value[:limit]
        ]
        if len(value) > limit:
            items.append({"omitted_count": len(value) - limit})
        return items
    if isinstance(value, str) and len(value) > 1200:
        return value[:1000] + "...[内容已压缩]..." + value[-120:]
    return value
