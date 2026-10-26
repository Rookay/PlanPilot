# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：agent.py
"""Agent 对话接口。"""

import time
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.agent.factory import agent, reconnect_agent
from app.agent.loop import STATE_OK
from app.config import llm_config
from app.database import get_db
from app.models.chat import ChatMessage
from app.schemas.agent import (
    AgentChatRequest,
    AgentChatResponse,
    ConfirmationInfoSchema,
)
from app.schemas.chat import ChatMessageResponse
from app.services import agent_run_service, chat_service, summary_service

router = APIRouter(
    prefix="/api/agent",
    tags=["Agent"],
)


@router.get("/status")
def agent_status():
    provider = agent.loop.llm
    status = agent.loop.llm_status
    return {
        "mode": "llm" if status.state == STATE_OK else "local",
        "provider": provider.__class__.__name__,
        "model": getattr(provider, "model", None),
        "state": status.state,
        "last_error_type": status.last_error_type,
        "last_error": status.last_error_message,
        "fallback_active": status.state != STATE_OK,
        "next_retry_at": (
            None
            if status.state == STATE_OK or status.fallback_until == float("inf")
            else status.fallback_until
        ),
        "consecutive_failures": status.consecutive_failures,
        "retry_after_seconds": agent.loop.retry_after_seconds(),
        "max_iterations": agent.loop.MAX_ITERATIONS,
        "max_tool_calls": agent.loop.MAX_TOOL_CALLS,
        "tool_call_limits": agent.loop.TOOL_CALL_LIMITS,
        "history_message_limit": agent.loop.HISTORY_MESSAGE_LIMIT,
        "history_token_budget": agent.loop.HISTORY_TOKEN_BUDGET,
        "history_message_token_limit": agent.loop.HISTORY_MESSAGE_TOKEN_LIMIT,
        "prompt_token_budget": agent.loop.PROMPT_TOKEN_BUDGET,
    }


@router.post("/reconnect")
def reconnect_model():
    return reconnect_agent()


@router.get("/sessions/{session_id}/messages", response_model=List[ChatMessageResponse])
def session_messages(session_id: str, db: Session = Depends(get_db)):
    return (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.id.asc())
        .all()
    )


@router.delete("/sessions/{session_id}/messages")
def clear_session_messages(session_id: str, db: Session = Depends(get_db)):
    deleted = chat_service.clear_messages(db, session_id)
    deleted_runs = agent_run_service.clear_runs(db, session_id)
    # 摘要必须一起删：它存的是被删掉那些消息的归纳，留着等于"清空会话"没清干净，
    # 下一轮还会把旧对话的约束重新注入 prompt。
    deleted_summary = summary_service.clear_summary(db, session_id)
    db.commit()
    # 没有进程内缓存要清理：历史每次请求都从表里读，删表即生效。
    return {
        "deleted": deleted,
        "deleted_runs": deleted_runs,
        "deleted_summary": deleted_summary,
    }


@router.get("/sessions/{session_id}/runs")
def session_runs(
    session_id: str,
    limit: int = 20,
    db: Session = Depends(get_db),
):
    return agent_run_service.list_runs(db, session_id, limit)


@router.get("/sessions/{session_id}/tool-calls")
def session_tool_calls(
    session_id: str,
    agent_run_id: Optional[str] = None,
    limit: int = 200,
    db: Session = Depends(get_db),
):
    """回看某个会话（或某次运行）调用过哪些工具、各自返回了什么。

    轨迹是审计用的，不参与对话上下文：把历史工具返回值再喂给模型，只会挤占
    预算并诱导它把过时的查询结果当成当前事实。
    """

    return [
        {
            "id": row.id,
            "agent_run_id": row.agent_run_id,
            "turn_index": row.turn_index,
            "tool_call_id": row.tool_call_id,
            "name": row.name,
            "arguments": row.arguments,
            "success": row.success,
            "summary": row.summary,
            "error": row.error,
            "result": row.result,
            "result_chars": row.result_chars,
            "created_at": row.created_at,
        }
        for row in chat_service.recent_tool_calls(
            db, session_id, agent_run_id=agent_run_id, limit=limit
        )
    ]


@router.post("/chat", response_model=AgentChatResponse)
def agent_chat(
    payload: AgentChatRequest,
    db: Session = Depends(get_db),
):
    session_id = payload.session_id or uuid.uuid4().hex

    started = time.perf_counter()
    result = agent.handle(
        db=db,
        message=payload.message,
        session_id=session_id,
        confirm_action_id=payload.confirm_action_id,
        decision=payload.decision,
    )
    duration_ms = int((time.perf_counter() - started) * 1000)
    # 场景以 loop 的实际结果为准：关键词命中后模型仍可能通过 select_skill 覆盖，
    # 这里若再自己 select 一次，展示给用户的会是错的那个。
    selected_skill = (
        agent.loop.skill_registry.get(result.selected_skill)
        if result.selected_skill and agent.loop.skill_registry is not None
        else None
    )
    provider_name = (
        "ConfirmationExecutor"
        if payload.confirm_action_id
        else agent.loop.last_run_provider or agent.loop.llm.__class__.__name__
    )
    leading_steps = []
    if payload.message.strip():
        leading_steps.append(
            {
                "kind": "context",
                "stage": "理解",
                "name": "intent_selection",
                "label": "识别请求",
                "status": "completed",
                "summary": (
                    f"已切换到「{selected_skill.description}」能力"
                    if selected_skill is not None and result.skill_source == "model"
                    else f"已选择「{selected_skill.description}」能力"
                    if selected_skill is not None
                    else "按通用规划请求处理"
                ),
                "duration_ms": 0,
            }
        )
    elif payload.confirm_action_id:
        leading_steps.append(
            {
                "kind": "confirmation",
                "stage": "确认",
                "name": "user_confirmation",
                "label": "处理用户确认",
                "status": "completed" if payload.decision == "accept" else "cancelled",
                "summary": "执行已确认操作" if payload.decision == "accept" else "用户取消操作",
                "duration_ms": 0,
            }
        )
    result.workflow_steps = leading_steps + result.workflow_steps
    if result.confirmation.required:
        result.workflow_steps.append(
            {
                "kind": "confirmation",
                "stage": "确认",
                "name": "await_confirmation",
                "label": "等待用户确认",
                "status": "waiting",
                "summary": result.confirmation.summary,
                "duration_ms": 0,
            }
        )
    statuses = {item.get("status") for item in result.workflow_steps}
    run_status = (
        "waiting_confirmation"
        if result.confirmation.required
        else "failed"
        if "failed" in statuses
        else "warning"
        if "warning" in statuses
        else "completed"
    )
    pricing = llm_config()
    estimated_cost = round(
        (result.model_usage.get("prompt_tokens", 0) / 1_000_000)
        * pricing["input_price_per_million"]
        + (result.model_usage.get("completion_tokens", 0) / 1_000_000)
        * pricing["output_price_per_million"],
        6,
    )
    run = agent_run_service.record_run(
        db,
        session_id=session_id,
        message=payload.message.strip(),
        provider=provider_name,
        skill=selected_skill.name if selected_skill is not None else None,
        status=run_status,
        duration_ms=duration_ms,
        confirmation_required=result.confirmation.required,
        steps=result.workflow_steps,
        model_usage=result.model_usage,
        estimated_cost=estimated_cost,
    )
    result.run_id = run.id
    result.duration_ms = duration_ms
    result.run_status = run_status

    if payload.message.strip():
        chat_service.append_message(
            db, session_id, "user", payload.message.strip()
        )
    # 工具轨迹和消息、运行记录同属一个事务。分开提交会出现"有轨迹但查不到回复"
    # 或反之的残片，回看时对不上号。
    chat_service.append_tool_calls(db, session_id, run.id, result.trajectory)
    chat_service.append_message(
        db, session_id, "assistant", result.reply, agent_run_id=run.id
    )
    db.commit()

    return AgentChatResponse(
        session_id=result.session_id,
        reply=result.reply,
        tool_calls=result.tool_calls,
        applied_changes=result.applied_changes,
        proposed_schedules=result.proposed_schedules,
        confirmation=ConfirmationInfoSchema(
            required=result.confirmation.required,
            action_id=result.confirmation.action_id,
            summary=result.confirmation.summary,
            changes=result.confirmation.changes,
            preview=result.confirmation.preview,
        ),
        workflow_steps=result.workflow_steps,
        run_id=result.run_id,
        duration_ms=result.duration_ms,
        run_status=result.run_status,
        model_usage=result.model_usage,
        estimated_cost=estimated_cost,
    )
