# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：loop.py
"""Agent Loop：LLM 决策 + 工具执行 + 结果观察 的循环。"""

import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

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
from app.agent.summary import fold_history
from app.llm.base import LLMProvider
from app.llm.exceptions import LLMAuthError, LLMRateLimitError, LLMTransientError
from app.llm.mock import MockProvider
from app.services import fact_service
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
    #: 本轮完整的工具调用轨迹（call 与 observation 成对），供 API 层落库。
    #: 与 ``tool_calls`` 的区别：``tool_calls`` 是给界面看的摘要，字段为展示
    #: 服务（stage / label）；``trajectory`` 保留协议原貌，用于回放与排查。
    trajectory: List[dict] = field(default_factory=list)


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


@dataclass
class _RunState:
    """单次请求的可变状态。

    这些字段原本是 ``_run_loop`` 里的十来个局部变量。抽出来的理由不是"少写
    几行参数"，而是让每个阶段方法只读写自己关心的字段——主循环因此能收回
    二十行，读的人不必同时记住十几个计数器的当前值。
    """

    db: Session
    session_id: str
    holder: dict
    messages: List[dict]
    provider: LLMProvider
    #: 当前生效的场景；``None`` 表示通用模式。模型可通过 select_skill 覆盖初值。
    selected_skill: Any = None
    #: 场景来源：keyword（规则先验）/ model（模型自选）/ none（通用模式）。
    skill_source: str = "none"
    #: 当前场景的工具白名单；``None`` 表示不限制。
    active_tool_names: Optional[set] = None
    skill_switch_count: int = 0
    tool_call_log: List[dict] = field(default_factory=list)
    applied_changes: List[dict] = field(default_factory=list)
    proposed_schedules: List[dict] = field(default_factory=list)
    tool_call_count: int = 0
    argument_parse_errors: int = 0
    #: 去重用的 "工具名+参数" 指纹集合。
    seen_calls: set = field(default_factory=set)
    tool_call_counts: dict = field(default_factory=dict)
    #: 本轮已用满预算的工具，下一轮不再出现在 tools 数组里。
    saturated_tools: set = field(default_factory=set)
    usage: dict = field(
        default_factory=lambda: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    )


@dataclass
class _PendingConfirm:
    """同一轮里攒下的待确认变更，最后合并成一次确认请求。"""

    calls: List[dict] = field(default_factory=list)
    summaries: List[str] = field(default_factory=list)
    previews: List[dict] = field(default_factory=list)


class AgentLoop:
    MAX_ITERATIONS = 12
    MAX_TOOL_CALLS = 24
    #: 单次请求的墙钟上限（秒）。``MAX_ITERATIONS × 单次 LLM 超时`` 只约束
    #: "一轮"，不约束"一次请求"：最坏能跑 6 分钟，而前端只有一个 loading。
    #: 这里给整次请求一个硬上限，超了就带着已完成的步骤提前收尾。
    #: 注意单次 LLM 调用无法从外部打断，所以实际最坏耗时会多出最多一次调用。
    RUN_DEADLINE_SECONDS = float(os.getenv("AGENT_RUN_DEADLINE_SECONDS", "90"))
    #: 历史条数上限，先按条数粗筛。
    HISTORY_MESSAGE_LIMIT = 10
    #: 单条历史消息的 token 上限，超出后按信息密度压缩。
    HISTORY_MESSAGE_TOKEN_LIMIT = 1200
    #: 整段历史的 token 预算。条数相同时，10 条短消息和 10 条长消息差别很大，
    #: 所以真正的闸门是 token 而不是条数。
    HISTORY_TOKEN_BUDGET = 3600
    #: 单次 prompt 的 token 总预算，超了就按"最大的工具结果优先降级"来削。
    PROMPT_TOKEN_BUDGET = 18000
    #: 读取消息窗口的上限。比保留窗口大得多：多出来的部分用来判断"该折多少进
    #: 摘要"，折完只把最后 HISTORY_MESSAGE_LIMIT 条原样发给模型。
    SUMMARY_WINDOW_MESSAGES = 60
    #: 触发折叠的最少溢出条数。太小会让摘要每轮重写——每折一次就是一次模型调用，
    #: 而摘要本身对细节是有损的，抖动着折反而更容易丢东西。
    SUMMARY_MIN_FOLD_MESSAGES = 8
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
        # 轨迹在这里统一收口。工具 observation 分散在十来处 append，逐处记录既容易
        # 漏，也会让主循环被审计代码淹没；改成把 messages 交给一个可变容器，等
        # 循环结束再统一配对。容器是请求内的局部对象，不是进程状态，并发安全。
        holder: dict = {}
        result = self._run_loop(db, message, session_id, holder)
        result.trajectory = self._extract_trajectory(
            holder.get("messages") or [],
            awaiting_ids=holder.get("awaiting_ids") or set(),
        )
        return result

    # ------------------------------------------------------------------
    # 单次请求的状态
    # ------------------------------------------------------------------

    def _begin_run(self, db: Session, message: str, session_id: str, holder: dict) -> _RunState:
        """准备一次请求的可变状态。

        不在这里登记用户消息：历史由 ``_build_messages`` 从 chat_messages 表按需
        读取，落库由 API 层统一在事务末尾完成。中间产生的 assistant / tool 消息只
        活在本次请求的 ``messages`` 里，不进入下一轮的上下文——但会作为轨迹落库，
        供回看与排查（不参与下一轮 prompt）。

        关键词路由只提供"先验"：命中就用，后续模型可通过 select_skill 覆盖。
        """
        selected_skill = (
            self.skill_registry.select(message) if self.skill_registry is not None else None
        )
        active_tool_names = None
        if selected_skill is not None and selected_skill.allowed_tools is not None:
            active_tool_names = set(selected_skill.allowed_tools)

        messages = self._build_messages(db, session_id, message, selected_skill)
        # 交给调用方持有引用：后续所有 append 都发生在这个同一个 list 上，
        # 循环提前 return 也能拿到截至目前完整的轨迹。
        holder["messages"] = messages

        return _RunState(
            db=db,
            session_id=session_id,
            holder=holder,
            messages=messages,
            provider=self.llm,
            selected_skill=selected_skill,
            skill_source="keyword" if selected_skill is not None else "none",
            active_tool_names=active_tool_names,
        )

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _run_loop(
        self,
        db: Session,
        message: str,
        session_id: str,
        holder: dict,
    ) -> AgentResult:
        state = self._begin_run(db, message, session_id, holder)
        started = time.monotonic()

        for _ in range(self.MAX_ITERATIONS):
            # 每轮先核预算：12 轮累积的工具结果可能远超单条上限。
            self._enforce_prompt_budget(state.messages)

            deadline = self._deadline_result(state, started)
            if deadline is not None:
                return deadline

            response = self._call_model(state)
            self._merge_usage(state.usage, response.usage)

            if response.is_text() or not response.tool_calls:
                return self._result(state, response.text or "（模型未返回内容）")

            # 先记录本轮 assistant 消息（原样回传 reasoning_content 等字段）。
            state.messages.append(self._assistant_message(response))

            pending = self._handle_tool_calls(state, response.tool_calls)
            if pending is not None:
                return pending

            if state.tool_call_count >= self.MAX_TOOL_CALLS:
                return self._reasoning_only_result(state)

        return self._reasoning_only_result(state)

    def _deadline_result(self, state: _RunState, started: float) -> Optional[AgentResult]:
        """达到单次请求墙钟上限时收尾；未超时返回 None。

        不调模型做总结：那正是要避免的额外等待，而且超时点通常已经是
        "预算耗尽"的状态，再问一次只会得到又一段冗长的计划。
        """
        elapsed = time.monotonic() - started
        if elapsed < self.RUN_DEADLINE_SECONDS:
            return None
        return self._result(
            state,
            f"这次请求已经跑了 {int(elapsed)} 秒，超过单次上限 "
            f"{int(self.RUN_DEADLINE_SECONDS)} 秒，我先停在这里。"
            f"已经完成 {state.tool_call_count} 次工具调用，结果都保留着；"
            "如果还差一步，继续告诉我就行。",
            workflow_steps=[
                {
                    "kind": "deadline",
                    "stage": "截断",
                    "name": "run_deadline",
                    "label": "达到单次请求时长上限",
                    "status": "warning",
                    "summary": (
                        f"已执行 {state.tool_call_count} 次工具调用后主动中止，"
                        "剩余步骤未执行。已完成的改动不会回滚。"
                    ),
                    "duration_ms": int(elapsed * 1000),
                }
            ],
        )

    def _reasoning_only_result(self, state: _RunState) -> AgentResult:
        """预算耗尽后的收尾：让模型基于已有观察做一次无工具总结。"""
        reply = self._finalize_without_tools(
            state.provider,
            state.messages,
            state.tool_call_log,
            state.proposed_schedules,
            state.usage,
        )
        return self._result(state, reply)

    def _result(
        self,
        state: _RunState,
        reply: str,
        *,
        confirmation: Optional[ConfirmationInfo] = None,
        workflow_steps: Optional[List[dict]] = None,
    ) -> AgentResult:
        """统一构造返回值。

        六个出口原本各拼一遍字段，任一出口漏掉 selected_skill 都会让界面
        退回"通用模式"、漏掉 applied_changes 就会把已生效的改动当成没发生。
        """
        return AgentResult(
            reply=reply,
            session_id=state.session_id,
            tool_calls=state.tool_call_log,
            applied_changes=state.applied_changes,
            proposed_schedules=state.proposed_schedules,
            model_usage=state.usage,
            selected_skill=(
                state.selected_skill.name if state.selected_skill is not None else None
            ),
            skill_source=state.skill_source,
            confirmation=confirmation or ConfirmationInfo(),
            workflow_steps=workflow_steps or [],
        )

    # ------------------------------------------------------------------
    # 模型调用与降级
    # ------------------------------------------------------------------

    def _call_model(self, state: _RunState):
        """调用当前可用的 provider，失败时降级到本地规则模式。

        三类失败走不同的熔断策略（鉴权失败永久禁用、限流与瞬时错误退避），
        但都要给模型留一条"为什么突然变成规则模式"的 system 消息——否则它
        会把降级后的行为当成正常上下文，继续照原计划解释。
        """
        provider = self._pick_provider()
        state.provider = provider
        tool_schemas = self._available_tool_schemas(
            state.saturated_tools, state.active_tool_names
        )
        try:
            self.last_run_provider = provider.__class__.__name__
            response = provider.chat(state.messages, tools=tool_schemas)
            if provider is self.llm:
                self._record_success()
            return response
        except LLMAuthError as exc:
            logger.warning("LLM authentication failed: %s", exc)
            self._mark_auth_failed(exc)
            return self._fallback_response(
                state, f"真实模型鉴权失败,已永久切换到本地规则模式。错误摘要:{exc}"
            )
        except (LLMRateLimitError, LLMTransientError) as exc:
            kind = "rate_limit" if isinstance(exc, LLMRateLimitError) else "transient"
            logger.warning("LLM %s, using fallback: %s", kind, exc)
            self._mark_failure(kind, exc)
            return self._fallback_response(
                state, f"真实模型暂时不可用，已切换到本地规则模式。错误摘要：{exc}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected LLM error, using fallback")
            self._mark_failure("transient", exc)
            return self._fallback_response(
                state, f"真实模型暂时不可用，已切换到本地规则模式。错误摘要：{exc}"
            )

    def _fallback_response(self, state: _RunState, notice: str):
        """降级到本地规则 provider，并把降级原因写进上下文。"""
        self.last_run_provider = self.fallback_provider.__class__.__name__
        state.messages.append({"role": "system", "content": notice})
        return self.fallback_provider.chat(
            state.messages,
            tools=self._available_tool_schemas(
                state.saturated_tools, state.active_tool_names
            ),
        )

    # ------------------------------------------------------------------
    # 工具调用处理
    # ------------------------------------------------------------------

    def _handle_tool_calls(
        self, state: _RunState, calls: List[dict]
    ) -> Optional[AgentResult]:
        """处理同一轮里的全部工具调用。

        返回非 None 表示这次请求要就此结束：参数解析连续失败，或者攒够了
        待确认的变更需要用户拍板。
        """
        pending = _PendingConfirm()
        for call in calls:
            stop = self._handle_tool_call(state, call, pending)
            if stop is not None:
                return stop
        if not pending.calls:
            return None
        return self._confirmation_result(state, pending)

    def _handle_tool_call(
        self, state: _RunState, call, pending: _PendingConfirm
    ) -> Optional[AgentResult]:
        """处理单个工具调用：校验参数、限次、预览或执行。"""
        tool = self.registry.get(call.name)
        call_key = self._call_key(call.name, call.arguments)

        # 参数不是合法 JSON 对象：不执行工具，把解析说明当作 Observation。
        # 必须与"字段缺失"分开——退化成空字典会让模型以为只是漏填，
        # 于是反复补字段而不去修语法；参数全可选的工具还会静默执行成宽松查询。
        if call.parse_error:
            return self._on_parse_error(state, call)

        if tool is None:
            state.messages.append(
                self._tool_message(call.id, call.name, False, f"未知工具：{call.name}")
            )
            state.tool_call_log.append(
                {
                    "stage": "执行",
                    "name": call.name,
                    "success": False,
                    "summary": f"未知工具：{call.name}",
                    "duration_ms": 0,
                }
            )
            return None

        # 场景切换：不占普通工具预算（那是业务执行预算），但单独限次。
        if call.name == self.SKILL_SWITCH_TOOL:
            self._handle_skill_switch(state, call)
            return None

        if call_key in state.seen_calls:
            state.saturated_tools.add(call.name)
            self._reject_call(
                state, call, "同一工具参数已经执行过，请根据已有结果直接作答"
            )
            return None
        state.seen_calls.add(call_key)

        if not self._consume_tool_budget(state, call):
            return None

        # confirm 级别：收集同一轮的全部变更，稍后合并成一次确认。
        if tool.confirmation == "confirm":
            self._handle_confirm_preview(state, call, pending)
            return None

        self._execute_tool_call(state, call, tool)
        return None

    def _on_parse_error(self, state: _RunState, call) -> Optional[AgentResult]:
        """参数不是合法 JSON：不执行，只回一条语法说明。

        连续坏 JSON 说明模型在这套协议上不稳定，再重试大概率还是错，直接转
        无工具总结，别把预算耗在语法上。返回非 None 表示就此结束。
        """
        state.argument_parse_errors += 1
        state.tool_call_count += 1
        state.messages.append(
            self._tool_message(call.id, call.name, False, error=call.parse_error)
        )
        state.tool_call_log.append(
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
        if state.argument_parse_errors < self.MAX_ARGUMENT_PARSE_ERRORS:
            return None
        return self._reasoning_only_result(state)

    def _reject_call(self, state: _RunState, call, reason: str) -> None:
        """拒绝一次调用：回 tool 消息并标记为受限，模型据此改用已有结果。"""
        state.messages.append(self._tool_message(call.id, call.name, False, error=reason))
        state.tool_call_log.append(
            {
                "stage": self._tool_stage(call.name),
                "name": call.name,
                "arguments": call.arguments,
                "success": False,
                "limited": True,
                "summary": reason,
                "duration_ms": 0,
            }
        )

    def _consume_tool_budget(self, state: _RunState, call) -> bool:
        """计入一次调用次数；已达上限时拒绝并返回 False。

        反向信号也在这里发：本轮用满即把工具名加进 saturated_tools，
        下一轮就不再把它放进 tools 数组——模型看不见它，比反复拒绝更省 token。
        """
        limit = self.TOOL_CALL_LIMITS.get(call.name)
        used = state.tool_call_counts.get(call.name, 0)
        if limit is not None and used >= limit:
            state.saturated_tools.add(call.name)
            self._reject_call(
                state,
                call,
                f"{call.name} 已达到本轮 {limit} 次调用上限；请使用已有结果完成回答",
            )
            return False
        state.tool_call_counts[call.name] = used + 1
        if limit is not None and used + 1 >= limit:
            state.saturated_tools.add(call.name)
        return True

    def _execute_tool_call(self, state: _RunState, call, tool) -> None:
        """执行工具、记录轨迹、把 observation 回给模型。"""
        execute_started = time.perf_counter()
        result = self.registry.execute(call.name, call.arguments, state.db)
        execute_duration = int((time.perf_counter() - execute_started) * 1000)
        state.tool_call_count += 1

        state.tool_call_log.append(
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
            state.applied_changes.append(
                {"name": call.name, "summary": result.summary, "data": result.data}
            )
            if (
                call.name in ("generate_plan", "replan")
                and isinstance(result.data, dict)
                and isinstance(result.data.get("schedules"), list)
            ):
                state.proposed_schedules = result.data["schedules"]

        state.messages.append(
            self._tool_message(
                call.id,
                call.name,
                result.success,
                result.summary,
                result.data,
                result.error,
            )
        )

    # ------------------------------------------------------------------
    # 场景切换
    # ------------------------------------------------------------------

    def _handle_skill_switch(self, state: _RunState, call) -> None:
        """处理场景切换：不占业务工具预算，但单独限次。

        每个分支都必须先回 tool 消息（OpenAI 协议要求 observation 紧跟
        tool_calls），成功时再追加新场景的指引。
        """
        state.skill_switch_count += 1
        if state.skill_switch_count > self.MAX_SKILL_SWITCHES:
            error = (
                f"本轮场景切换已达 {self.MAX_SKILL_SWITCHES} 次上限，"
                "请基于当前场景直接作答"
            )
            state.messages.append(self._tool_message(call.id, call.name, False, error=error))
            state.tool_call_log.append(
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
            return

        result = self.registry.execute(call.name, call.arguments, state.db)
        target = self._resolve_skill(result)

        if target is None:
            reason = result.error or "场景切换失败"
            state.messages.append(
                self._tool_message(call.id, call.name, False, result.summary, None, reason)
            )
            self._log_skill_switch(state, call, success=False, summary=reason)
            return

        if state.selected_skill is not None and target.name == state.selected_skill.name:
            already = f"当前已在「{target.description}」场景，无需重复切换"
            state.messages.append(
                self._tool_message(call.id, call.name, True, already, {"name": target.name})
            )
            self._log_skill_switch(state, call, success=True, summary=already)
            return

        state.selected_skill = target
        state.skill_source = "model"
        state.active_tool_names = (
            set(target.allowed_tools) if target.allowed_tools is not None else None
        )
        state.messages.append(
            self._tool_message(call.id, call.name, True, result.summary, result.data)
        )
        state.messages.append(
            {"role": "system", "content": self._skill_hint_message(target)}
        )
        self._log_skill_switch(state, call, success=True, summary=result.summary)

    def _log_skill_switch(self, state: _RunState, call, *, success: bool, summary) -> None:
        state.tool_call_log.append(
            {
                "stage": "理解",
                "name": call.name,
                "arguments": call.arguments,
                "success": success,
                "summary": summary,
                "duration_ms": 0,
            }
        )

    # ------------------------------------------------------------------
    # 待确认变更
    # ------------------------------------------------------------------

    def _handle_confirm_preview(
        self, state: _RunState, call, pending: _PendingConfirm
    ) -> None:
        """confirm 级工具：只生成预览，把变更攒进 pending 等合并确认。

        不占 ``tool_call_count``——那是"已执行"的预算，预览还没动数据。
        """
        preview_started = time.perf_counter()
        preview = self.registry.preview(call.name, call.arguments, state.db)
        preview_duration = int((time.perf_counter() - preview_started) * 1000)

        if not preview.success:
            state.messages.append(
                self._tool_message(
                    call.id, call.name, False, error=preview.error or "无法生成操作预览"
                )
            )
            state.tool_call_log.append(
                {
                    "name": call.name,
                    "arguments": call.arguments,
                    "success": False,
                    "summary": preview.error,
                    "duration_ms": preview_duration,
                }
            )
            return

        pending.calls.append({"name": call.name, "args": call.arguments})
        # 记下 id：这轮结束后 messages 里不会有它的 observation，
        # 轨迹采集需要据此把它标成"等确认"而不是"被中断"。
        state.holder.setdefault("awaiting_ids", set()).add(call.id)
        pending.summaries.append(preview.summary or f"即将执行 {call.name}")
        if preview.data is not None:
            pending.previews.append(preview.data)
        state.tool_call_log.append(
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

    def _confirmation_result(
        self, state: _RunState, pending: _PendingConfirm
    ) -> AgentResult:
        """把本轮攒下的变更合并成一次确认请求。

        多个变更合成一个 action_id：用户点一次确认就走完，避免逐个弹窗，
        也让补偿事务有一个明确的原子边界。
        """
        if len(pending.summaries) == 1:
            summary = pending.summaries[0]
        else:
            summary = (
                f"将执行 {len(pending.summaries)} 项操作：" + "；".join(pending.summaries)
            )
        combined_preview = (
            pending.previews[0]
            if len(pending.previews) == 1
            else {"items": pending.previews}
        )
        action_id = self.confirmation_store.create(
            db=state.db,
            tool_calls=pending.calls,
            summary=summary,
            preview=combined_preview,
            session_id=state.session_id,
        )
        return self._result(
            state,
            f"需要你确认：{summary}",
            confirmation=ConfirmationInfo(
                required=True,
                action_id=action_id,
                summary=summary,
                changes=[
                    {"name": item["name"], "arguments": item["args"]}
                    for item in pending.calls
                ],
                preview=combined_preview,
            ),
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

        # 长期事实排在这轮摘要之前：事实是跨会话稳定的（称呼、固定安排），
        # 摘要只管本次会话。放前面等于告诉模型"先拿这些当背景，再看最近聊了什么"。
        facts_text = fact_service.render_facts(fact_service.confirmed_facts(db))
        if facts_text:
            system += "\n\n" + facts_text

        # 历史从表里读，不查进程内缓存：多 worker 与进程重启都不会丢上下文。
        # 当前这轮的用户消息还没落库（事务在 API 层末尾才提交），单独补在末尾。
        #
        # 顺序是"先折叠、再裁剪"：折叠把溢出窗口的消息变成摘要（信息保住了），
        # 裁剪只是按 token 兜底。反过来做的话，裁剪会先把消息扔掉，等轮到折叠时
        # 它们已经不在了——摘要里永远不会出现，约束就此丢失。
        folded = fold_history(
            db,
            self._pick_provider(),
            session_id,
            window_limit=self.SUMMARY_WINDOW_MESSAGES,
            keep_messages=self.HISTORY_MESSAGE_LIMIT,
            min_fold_messages=self.SUMMARY_MIN_FOLD_MESSAGES,
        )
        if folded.summary_text:
            system += "\n\n" + folded.summary_text

        history, dropped = self._prepare_history(folded.history)
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

    #: 轨迹里单条 observation 的落库上限。工具结果本身已经过 ``compact_payload``，
    #: 但地图路线一类仍可能到几 KB；审计需要的是"调了什么、成没成、关键返回"，
    #: 不是把整个响应体灌进业务库。
    TRAJECTORY_RESULT_CHARS = 4000

    @classmethod
    def _extract_trajectory(
        cls,
        messages: List[dict],
        awaiting_ids: Optional[set] = None,
    ) -> List[dict]:
        """把局部 messages 里的 ``assistant.tool_calls`` 与 ``tool`` 消息配对。

        配对失败的调用也保留（``success=False`` + error 说明）。截断本身是排查
        线索——"模型调了但没拿到结果"和"模型根本没调"是两种完全不同的故障，
        悄悄丢掉前者会让人误判成因。

        ``awaiting_ids`` 是"被 confirm 级别拦下、正在等用户确认"的调用 id。
        这类调用同样没有 observation，但原因不是中断而是刻意挂起：把两者混成
        同一条记录，回看时会把一次正常待确认误读成故障，也会掩盖真正的中断。
        """

        awaiting = awaiting_ids or set()
        records: List[dict] = []
        pending: dict = {}

        for message in messages:
            if not isinstance(message, dict):
                continue

            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function") or {}
                    call_id = str(call.get("id") or "")
                    is_awaiting = call_id in awaiting
                    record = {
                        "turn_index": len(records),
                        "tool_call_id": call_id,
                        "name": str(function.get("name") or ""),
                        "arguments": cls._as_text(function.get("arguments")),
                        "success": False,
                        "summary": "等待用户确认，尚未执行" if is_awaiting else None,
                        "error": None
                        if is_awaiting
                        else "本轮未返回结果（调用被中断）",
                        "result": None,
                        "result_chars": 0,
                    }
                    records.append(record)
                    pending[call_id] = record
                continue

            if message.get("role") != "tool":
                continue

            call_id = str(message.get("tool_call_id") or "")
            record = pending.pop(call_id, None)
            if record is None:
                # 有 observation 却没有对应 call。协议上不该出现，但既然消息里
                # 有结果就说明确实执行过，仍要留痕，否则看起来像凭空多了一次执行。
                record = {
                    "turn_index": len(records),
                    "tool_call_id": call_id,
                    "name": str(message.get("name") or ""),
                    "arguments": "",
                    "success": False,
                    "summary": None,
                    "error": None,
                    "result": None,
                    "result_chars": 0,
                }
                records.append(record)

            content = cls._as_text(message.get("content"))
            record["result_chars"] = len(content)
            record["result"] = (
                content
                if len(content) <= cls.TRAJECTORY_RESULT_CHARS
                else content[: cls.TRAJECTORY_RESULT_CHARS] + "…[轨迹落库时截断]"
            )
            record["success"], record["summary"], record["error"] = cls._read_observation(
                content
            )

        return records

    @staticmethod
    def _as_text(value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _read_observation(content: str) -> tuple[bool, Optional[str], Optional[str]]:
        """从 observation 文本里取出 ``(success, summary, error)``。

        不是 JSON 时按"执行过但格式异常"处理：``success`` 记 False，原文进
        summary。这条路径不该出现，真出现时原文比空值有用。
        """

        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return False, content[:500] or None, "observation 不是合法 JSON"
        if not isinstance(parsed, dict):
            return False, str(parsed)[:500], "observation 不是 JSON 对象"

        summary = parsed.get("summary")
        error = parsed.get("error")
        return (
            bool(parsed.get("success")),
            None if summary is None else str(summary),
            None if error is None else str(error),
        )

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
