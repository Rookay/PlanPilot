# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：agent.py
"""Agent 对话接口。"""

import uuid
import time
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.agent.factory import agent
from app.database import get_db
from app.models.chat import ChatMessage
from app.schemas.agent import (
    AgentChatRequest,
    AgentChatResponse,
    ConfirmationInfoSchema,
)
from app.schemas.chat import ChatMessageResponse

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
        "max_iterations": agent.loop.MAX_ITERATIONS,
        "max_tool_calls": agent.loop.MAX_TOOL_CALLS,
    }


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
    db.commit()
    agent.memory.clear(session_id)
    return {"deleted": deleted}


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

    result = agent.handle(
        db=db,
        message=payload.message,
        session_id=session_id,
        confirm_action_id=payload.confirm_action_id,
        decision=payload.decision,
    )

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
    )
