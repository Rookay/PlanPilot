# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：agent.py
"""Agent 对话接口。"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.agent.factory import agent
from app.database import get_db
from app.schemas.agent import (
    AgentChatRequest,
    AgentChatResponse,
    ConfirmationInfoSchema,
)

router = APIRouter(
    prefix="/api/agent",
    tags=["Agent"],
)


@router.post("/chat", response_model=AgentChatResponse)
def agent_chat(
    payload: AgentChatRequest,
    db: Session = Depends(get_db),
):
    session_id = payload.session_id or uuid.uuid4().hex

    result = agent.handle(
        db=db,
        message=payload.message,
        session_id=session_id,
        confirm_action_id=payload.confirm_action_id,
        decision=payload.decision,
    )

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
    )
