# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：agent.py
"""Agent 对话接口。"""

import uuid
import time
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.agent.factory import agent, reconnect_agent
from app.config import llm_config
from app.database import get_db
from app.models.chat import ChatMessage
from app.schemas.agent import (
    AgentChatRequest,
    AgentChatResponse,
    ConfirmationInfoSchema,
)
from app.schemas.chat import ChatMessageResponse
from app.services import agent_run_service

router = APIRouter(
    prefix="/api/agent",
    tags=["Agent"],
)


@router.get("/status")
def agent_status():
    provider = agent.loop.llm
    is_fallback = provider.__class__.__name__ == "MockProvider"
    return {
        "mode": "local" if is_fallback else "llm",
        "provider": provider.__class__.__name__,
        "model": getattr(provider, "model", None),
        "fallback_active": time.monotonic() < agent.loop.fallback_until,
        "last_error": agent.loop.last_provider_error,
        "retry_after_seconds": agent.loop.retry_after_seconds(),
        "max_iterations": agent.loop.MAX_ITERATIONS,
        "max_tool_calls": agent.loop.MAX_TOOL_CALLS,
        "tool_call_limits": agent.loop.TOOL_CALL_LIMITS,
        "history_message_limit": agent.loop.HISTORY_MESSAGE_LIMIT,
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
    deleted = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .delete(synchronize_session=False)
    )
    deleted_runs = agent_run_service.clear_runs(db, session_id)
    db.commit()
    agent.memory.clear(session_id)
    return {"deleted": deleted, "deleted_runs": deleted_runs}


@router.get("/sessions/{session_id}/runs")
def session_runs(
    session_id: str,
    limit: int = 20,
    db: Session = Depends(get_db),
):
    return agent_run_service.list_runs(db, session_id, limit)


@router.post("/chat", response_model=AgentChatResponse)
def agent_chat(
    payload: AgentChatRequest,
    db: Session = Depends(get_db),
):
    session_id = payload.session_id or uuid.uuid4().hex

    if not agent.memory.has_session(session_id):
        persisted = (
            db.query(ChatMessage)
            .filter(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id.asc())
            .all()
        )
        agent.memory.hydrate(
            session_id,
            [{"role": item.role, "content": item.content} for item in persisted],
        )

    selected_skill = (
        agent.loop.skill_registry.select(payload.message)
        if payload.message.strip() and agent.loop.skill_registry is not None
        else None
    )
    started = time.perf_counter()
    result = agent.handle(
        db=db,
        message=payload.message,
        session_id=session_id,
        confirm_action_id=payload.confirm_action_id,
        decision=payload.decision,
    )
    duration_ms = int((time.perf_counter() - started) * 1000)
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
                    f"已选择「{selected_skill.description}」能力"
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
        db.add(
            ChatMessage(
                session_id=session_id,
                role="user",
                content=payload.message.strip(),
            )
        )
    db.add(
        ChatMessage(
            session_id=session_id,
            role="assistant",
            content=result.reply,
            agent_run_id=run.id,
        )
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
