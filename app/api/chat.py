# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：chat.py
"""对话接口。"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.chat import ChatMessage
from app.schemas.chat import (
    ChatMessageResponse,
    ChatSendRequest,
    ChatSendResponse,
)
from app.services.chat_service import generate_reply

router = APIRouter(
    prefix="/api/chat",
    tags=["Chat"],
)


@router.get(
    "/messages",
    response_model=List[ChatMessageResponse],
)
def list_messages(
    db: Session = Depends(get_db),
):
    """返回全部历史消息，按时间顺序排列。"""

    return (
        db.query(ChatMessage)
        .order_by(ChatMessage.id.asc())
        .all()
    )


@router.post(
    "/messages",
    response_model=ChatSendResponse,
)
def send_message(
    payload: ChatSendRequest,
    db: Session = Depends(get_db),
):
    """保存用户消息，并返回助手回复。"""

    content = payload.content
    if not content:
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    user_message = ChatMessage(role="user", content=content)
    db.add(user_message)
    db.flush()  # 先拿到用户消息的 id，方便连同上下文一起传给服务。

    history = [
        {"role": message.role, "content": message.content}
        for message in db.query(ChatMessage).order_by(ChatMessage.id.asc()).all()
    ]

    reply = generate_reply(content, history)

    assistant_message = ChatMessage(role="assistant", content=reply)
    db.add(assistant_message)
    db.commit()
    db.refresh(user_message)
    db.refresh(assistant_message)

    return ChatSendResponse(
        user_message=user_message,
        assistant_message=assistant_message,
    )


@router.delete("/messages")
def clear_messages(
    db: Session = Depends(get_db),
):
    """清空全部对话记录。"""

    deleted = (
        db.query(ChatMessage)
        .delete(synchronize_session=False)
    )
    db.commit()

    return {"deleted": deleted}
