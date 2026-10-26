# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：loop.py
"""Agent Loop：LLM 决策 + 工具执行 + 结果观察 的循环。"""

import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.orm import Session

from app.agent.compaction import (
    compact_payload,
    degrade_tool_message,
    estimate_message_tokens,
    estimate_messages_tokens,
    prepare_history,
)
from app.agent.confirmation import PendingActionStore
from app.agent.context import build_system_prompt, build_world_summary
from app.agent.memory import Memory
from app.llm.base import LLMProvider
from app.llm.exceptions import LLMAuthError, LLMRateLimitError, LLMTransientError
from app.llm.mock import MockProvider
from app.services import chat_service
from app.skills.base import SkillRegistry
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

STATE_OK = "ok"
STATE_TRANSIENT = "transient_fallback"
STATE_RATE_LIMITED = "rate_limited"
STATE_AUTH_FAILED = "auth_failed"


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
    #: 本轮实际生效的场景标识；由关键词命中或模型 select_skill 切换得到。
    selected_skill: Optional[str] = None
    #: 场景来源：keyword（规则先验）/ model（模型自选）/ none（通用模式）。
    skill_source: str = "none"


@dataclass
class LLMStatus:
    """LLM Provider 的运行状态,由 AgentLoop 维护并通过 /api/agent/status 暴露。

    - state 取值:
        "ok"                 —— 真实 LLM 正常(或从未出过错)
        "transient_fallback" —— 上次失败是 5xx / 网络 / 超时,按指数退避中
        "rate_limited"       —— 上次失败是 429,按指数退避中
        "auth_failed"        —— 上次失败是 401 / 403,永久禁用直到进程重启
    - fallback_until 是 ``time.monotonic()`` 时刻;``float("inf")`` 表示永不复原。
    """

    state: str = STATE_OK
    consecutive_failures: int = 0
    last_error_type: Optional[str] = None  # auth / rate_limit / transient
    last_error_message: Optional[str] = None
    last_success_at: Optional[float] = None
    fallback_until: float = 0.0


class AgentLoop:
    MAX_ITERATIONS = 12
    MAX_TOOL_CALLS = 24
    #: 历史条数上限，先按条数粗筛。
    HISTORY_MESSAGE_LIMIT = 10
    #: 单条历史消息的 token 上限，超出后按信息密度压缩。
    HISTORY_MESSAGE_TOKEN_LIMIT = 1200
    #: 整段历史的 token 预算。条数相同时，10 条短消息和 10 条长消息差别很大，
    #: 所以真正的闸门是 token 而不是条数。
    HISTORY_TOKEN_BUDGET = 3600
    #: 单次 prompt 的 token 总预算，超了就按"最大的工具结果优先降级"来削。
    PROMPT_TOKEN_BUDGET = 18000
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
        "compare_plan_alternatives": 1,
        "apply_plan_alternative": 1,
        "search_places": 8,
        "geocode_address": 8,
        "estimate_travel_time": 12,
    }
    BACKOFF_BASE_SECONDS = 60
    BACKOFF_MAX_SECONDS = 600
    BACKOFF_JITTER_RATIO = 0.2

    #: 场景切换工具：无论当前场景白名单如何收窄都必须保持可见，
    #: 否则模型切换到收窄场景后无法再切回，形成死锁。
    SKILL_SWITCH_TOOL = "select_skill"
    ALWAYS_AVAILABLE_TOOLS = {SKILL_SWITCH_TOOL}
    #: 单轮允许的切换次数，防止模型在场景之间来回横跳。
    MAX_SKILL_SWITCHES = 3
    #: 单轮允许的参数解析失败次数。连续给出坏 JSON 说明模型在这套协议上不稳定，
    #: 再重试大概率还是错，直接转无工具总结，别把预算耗在语法上。
    MAX_ARGUMENT_PARSE_ERRORS = 2

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
        self.llm_status = LLMStatus()
        self.last_run_provider: Optional[str] = None

    def run(self, db: Session, message: str, session_id: str) -> AgentResult:
        # 不在这里登记用户消息：历史由 _build_messages 从 chat_messages 表按需读取，
        # 落库由 API 层统一在事务末尾完成。中间产生的 assistant / tool 消息只活在
        # 本轮局部变量 messages 里，不进入下一轮的上下文——工具调用轨迹对下一次
        # 请求没有价值，带上反而挤占预算。

        # 关键词路由只提供"先验"：命中就用，后续模型可通过 select_skill 覆盖。
        selected_skill = (
            self.skill_registry.select(message)
            if self.skill_registry is not None
            else None
        )
        skill_source = "keyword" if selected_skill is not None else "none"
        skill_switch_count = 0
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
        argument_parse_errors = 0
        seen_calls: set[str] = set()
        tool_call_counts: dict[str, int] = {}
        saturated_tools: set[str] = set()
        model_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        provider = self.llm

        for _ in range(self.MAX_ITERATIONS):
            # 每轮先核预算：12 轮累积的工具结果可能远超单条上限。
            self._enforce_prompt_budget(messages)
            provider = self._pick_provider()
            used_real = provider is self.llm
            try:
                self.last_run_provider = provider.__class__.__name__
                response = provider.chat(
                    messages,
                    tools=self._available_tool_schemas(
                        saturated_tools,
                        active_tool_names,
                    ),
                )
                if used_real:
                    self._record_success()
            except LLMAuthError as exc:
                logger.warning("LLM authentication failed: %s", exc)
                self._mark_auth_failed(exc)
                self.last_run_provider = self.fallback_provider.__class__.__name__
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "真实模型鉴权失败,已永久切换到本地规则模式。"
                            f"错误摘要:{exc}"
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
            except (LLMRateLimitError, LLMTransientError) as exc:
                kind = "rate_limit" if isinstance(exc, LLMRateLimitError) else "transient"
                logger.warning("LLM %s, using fallback: %s", kind, exc)
                self._mark_failure(kind, exc)
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
            except Exception as exc:  # noqa: BLE001
                logger.exception("Unexpected LLM error, using fallback")
                self._mark_failure("transient", exc)
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
                return AgentResult(
                    reply=reply,
                    session_id=session_id,
                    tool_calls=tool_call_log,
                    applied_changes=applied_changes,
                    proposed_schedules=proposed_schedules,
                    model_usage=model_usage,
                    selected_skill=selected_skill.name if selected_skill is not None else None,
                    skill_source=skill_source,
                )

            # 先记录本轮 assistant 消息（原样回传 reasoning_content 等字段）。
            messages.append(self._assistant_message(response))

            pending_confirm_calls: List[dict] = []
            pending_summaries: List[str] = []
            pending_previews: List[dict] = []

            for call in response.tool_calls:
                tool = self.registry.get(call.name)
                call_key = self._call_key(call.name, call.arguments)

                # 参数不是合法 JSON 对象：不执行工具，把解析说明当作 Observation。
                # 必须与"字段缺失"分开——退化成空字典会让模型以为只是漏填，
                # 于是反复补字段而不去修语法；参数全可选的工具还会静默执行成宽松查询。
                if call.parse_error:
                    argument_parse_errors += 1
                    tool_call_count += 1
                    messages.append(
                        self._tool_message(call.id, call.name, False, error=call.parse_error)
                    )
                    tool_call_log.append(
                        {
                            "stage": "理解",
                            "name": call.name,
                            "arguments": call.arguments,
                            "success": False,
                            "summary": call.parse_error,
                            "duration_ms": 0,
                            "argument_error": True,
                        }
                    )
                    if argument_parse_errors >= self.MAX_ARGUMENT_PARSE_ERRORS:
                        reply = self._finalize_without_tools(
                            provider,
                            messages,
                            tool_call_log,
                            proposed_schedules,
                            model_usage,
                        )
                        return AgentResult(
                            reply=reply,
                            session_id=session_id,
                            tool_calls=tool_call_log,
                            applied_changes=applied_changes,
                            proposed_schedules=proposed_schedules,
                            model_usage=model_usage,
                            selected_skill=(
                                selected_skill.name if selected_skill is not None else None
                            ),
                            skill_source=skill_source,
                        )
                    continue

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

                # 场景切换：不占普通工具预算（那是业务执行预算），但单独限次。
                if call.name == self.SKILL_SWITCH_TOOL:
                    skill_switch_count += 1
                    if skill_switch_count > self.MAX_SKILL_SWITCHES:
                        error = (
                            f"本轮场景切换已达 {self.MAX_SKILL_SWITCHES} 次上限，"
                            "请基于当前场景直接作答"
                        )
                        messages.append(
                            self._tool_message(call.id, call.name, False, error=error)
                        )
                        tool_call_log.append(
                            {
                                "stage": "理解",
                                "name": call.name,
                                "arguments": call.arguments,
                                "success": False,
                                "limited": True,
                                "summary": error,
                                "duration_ms": 0,
                            }
                        )
                        continue

                    result = self.registry.execute(call.name, call.arguments, db)
                    target = self._resolve_skill(result)
                    if target is None:
                        messages.append(
                            self._tool_message(
                                call.id,
                                call.name,
                                False,
                                result.summary,
                                None,
                                result.error or "场景切换失败",
                            )
                        )
                        tool_call_log.append(
                            {
                                "stage": "理解",
                                "name": call.name,
                                "arguments": call.arguments,
                                "success": False,
                                "summary": result.error or "场景切换失败",
                                "duration_ms": 0,
                            }
                        )
                        continue

                    if selected_skill is not None and target.name == selected_skill.name:
                        already = f"当前已在「{target.description}」场景，无需重复切换"
                        messages.append(
                            self._tool_message(
                                call.id, call.name, True, already, {"name": target.name}
                            )
                        )
                        tool_call_log.append(
                            {
                                "stage": "理解",
                                "name": call.name,
                                "arguments": call.arguments,
                                "success": True,
                                "summary": already,
                                "duration_ms": 0,
                            }
                        )
                        continue

                    selected_skill = target
                    skill_source = "model"
                    active_tool_names = (
                        set(target.allowed_tools)
                        if target.allowed_tools is not None
                        else None
                    )
                    # 先回 tool 结果（OpenAI 协议要求紧跟 tool_calls），再追加场景指引。
                    messages.append(
                        self._tool_message(
                            call.id, call.name, True, result.summary, result.data
                        )
                    )
                    messages.append(
                        {"role": "system", "content": self._skill_hint_message(target)}
                    )
                    tool_call_log.append(
                        {
                            "stage": "理解",
                            "name": call.name,
                            "arguments": call.arguments,
                            "success": True,
                            "summary": result.summary,
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
                    db=db,
                    tool_calls=pending_confirm_calls,
                    summary=confirmation_summary,
                    preview=combined_preview,
                    session_id=session_id,
                )
                confirmation_reply = f"需要你确认：{confirmation_summary}"
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
                    selected_skill=selected_skill.name if selected_skill is not None else None,
                    skill_source=skill_source,
                )

            if tool_call_count >= self.MAX_TOOL_CALLS:
                reply = self._finalize_without_tools(
                    provider,
                    messages,
                    tool_call_log,
                    proposed_schedules,
                    model_usage,
                )
                return AgentResult(
                    reply=reply,
                    session_id=session_id,
                    tool_calls=tool_call_log,
                    applied_changes=applied_changes,
                    proposed_schedules=proposed_schedules,
                    model_usage=model_usage,
                    selected_skill=selected_skill.name if selected_skill is not None else None,
                    skill_source=skill_source,
                )

        reply = self._finalize_without_tools(
            provider,
            messages,
            tool_call_log,
            proposed_schedules,
            model_usage,
        )
        return AgentResult(
            reply=reply,
            session_id=session_id,
            tool_calls=tool_call_log,
            applied_changes=applied_changes,
            proposed_schedules=proposed_schedules,
            model_usage=model_usage,
            selected_skill=selected_skill.name if selected_skill is not None else None,
            skill_source=skill_source,
        )

    def replace_provider(self, provider: LLMProvider) -> None:
        self.llm = provider
        self.mark_provider_available()

    def mark_provider_available(self) -> None:
        self._record_success()

    def mark_provider_unavailable(self, error: Exception | str) -> None:
        self._mark_failure(
            "transient",
            error if isinstance(error, Exception) else RuntimeError(error),
        )

    def retry_after_seconds(self) -> int:
        if self.llm_status.fallback_until == float("inf"):
            return 0
        return max(
            0,
            int(self.llm_status.fallback_until - time.monotonic() + 0.999),
        )

    @property
    def fallback_until(self) -> float:
        return self.llm_status.fallback_until

    @property
    def last_provider_error(self) -> Optional[str]:
        return self.llm_status.last_error_message

    def _pick_provider(self) -> LLMProvider:
        if (
            self.llm_status.state != STATE_OK
            and time.monotonic() < self.llm_status.fallback_until
        ):
            return self.fallback_provider
        return self.llm

    def _mark_failure(self, error_type: str, error: Exception) -> None:
        status = self.llm_status
        status.consecutive_failures += 1
        status.state = (
            STATE_RATE_LIMITED if error_type == "rate_limit" else STATE_TRANSIENT
        )
        status.last_error_type = error_type
        status.last_error_message = str(error)
        base_delay = self.BACKOFF_BASE_SECONDS * (2 ** (status.consecutive_failures - 1))
        delay = min(base_delay, self.BACKOFF_MAX_SECONDS)
        jitter = random.uniform(0, delay * self.BACKOFF_JITTER_RATIO)
        status.fallback_until = time.monotonic() + delay + jitter

    def _mark_auth_failed(self, error: Exception) -> None:
        status = self.llm_status
        status.state = STATE_AUTH_FAILED
        status.last_error_type = "auth"
        status.last_error_message = str(error)
        status.fallback_until = float("inf")

    def _record_success(self) -> None:
        self.llm_status = LLMStatus(last_success_at=time.monotonic())

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
            system += (
                f"\n\n【当前场景】{selected_skill.name}（{selected_skill.description}）。"
                "这是按关键词自动匹配的结果；如果它与用户真实意图不符，"
                "调用 select_skill 切换到更合适的场景。"
            )
        elif self.skill_registry is not None:
            system += (
                "\n\n【当前场景】未匹配到预设场景，使用通用模式。"
                "如果请求明显属于某个业务场景（可在 select_skill 的场景清单中查看），"
                "先调用 select_skill 指定场景，再执行操作。"
            )

        system += "\n\n当前数据快照：\n" + build_world_summary(db, timezone)

        # 历史从表里读，不查进程内缓存：多 worker 与进程重启都不会丢上下文。
        # 当前这轮的用户消息还没落库（事务在 API 层末尾才提交），单独补在末尾。
        history, dropped = self._prepare_history(
            chat_service.recent_messages(
                db, session_id, limit=self.HISTORY_MESSAGE_LIMIT
            )
        )
        if dropped:
            system += (
                f"\n\n（对话历史较长，最早 {dropped} 条已省略。"
                "涉及此前细节时不要凭印象回答，用工具重新查询。）"
            )

        messages: List[dict] = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": message})
        return messages

    def _resolve_skill(self, result):
        """从 select_skill 的工具结果里取出目标 Skill（不信任结果里的 hint 文本）。"""

        if not result.success or not isinstance(result.data, dict):
            return None
        if self.skill_registry is None:
            return None
        return self.skill_registry.get(str(result.data.get("name") or ""))

    @staticmethod
    def _skill_hint_message(skill) -> str:
        return (
            f"【场景已切换】当前场景：{skill.name}（{skill.description}）。"
            "以下指引优先于此前任何场景指引：\n"
            f"{skill.system_hint}"
        )

    def _available_tool_schemas(
        self,
        saturated_tools: set[str],
        active_tool_names: Optional[set[str]] = None,
    ) -> List[dict]:
        available = []
        for schema in self.registry.schemas():
            name = schema.get("function", {}).get("name")
            if name in saturated_tools:
                continue
            # 场景切换工具必须在白名单收窄后仍然可见，否则无法切回。
            if name in self.ALWAYS_AVAILABLE_TOOLS:
                available.append(schema)
                continue
            if active_tool_names is not None and name not in active_tool_names:
                continue
            available.append(schema)
        return available

    def _prepare_history(self, messages: List[dict]) -> tuple[List[dict], int]:
        """按 token 总预算从最新往回保留历史，返回 (消息, 被丢弃条数)。

        真正的闸门是 token 而不是条数：10 条短消息和 10 条长消息差一个数量级。
        """

        return prepare_history(
            messages,
            message_token_limit=self.HISTORY_MESSAGE_TOKEN_LIMIT,
            total_token_limit=self.HISTORY_TOKEN_BUDGET,
        )

    def _enforce_prompt_budget(self, messages: List[dict]) -> int:
        """prompt 超预算时，按"体积最大优先"降级工具结果。

        只降级 ``role == "tool"`` 的消息，且保留 success / summary / error——
        协议要求每个 tool_call_id 都有对应的 tool 消息，所以只能瘦身不能删除。
        返回削减后的估算 token 数。
        """

        total = estimate_messages_tokens(messages)
        if total <= self.PROMPT_TOKEN_BUDGET:
            return total

        indexes = [
            index
            for index, message in enumerate(messages)
            if message.get("role") == "tool"
        ]
        indexes.sort(key=lambda i: estimate_message_tokens(messages[i]), reverse=True)

        for index in indexes:
            if total <= self.PROMPT_TOKEN_BUDGET:
                break
            before = estimate_message_tokens(messages[index])
            degraded = degrade_tool_message(messages[index])
            if degraded is None:
                continue
            messages[index] = degraded
            total -= before - estimate_message_tokens(messages[index])

        if total > self.PROMPT_TOKEN_BUDGET:
            logger.warning(
                "prompt 仍超出预算：%s > %s tokens（已降级所有工具结果）",
                total,
                self.PROMPT_TOKEN_BUDGET,
            )
        return total

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
            "compare_plan_alternatives",
            "apply_plan_alternative",
            "create_events",
            "revise_trip",
        }:
            return "规划"
        return "执行"

    @staticmethod
    def tool_label(name: str) -> str:
        labels = {
            "select_skill": "切换场景",
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
            "compare_plan_alternatives": "比较规划方案",
            "apply_plan_alternative": "应用规划方案",
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
                    "data": compact_payload(data),
                    "error": error,
                },
                ensure_ascii=False,
                default=str,
            ),
        }
