# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：chat_request.py
"""对话请求的去重记录，用于幂等。

一次 ``POST /api/agent/chat`` 可能跑十几轮工具调用、花几十秒。这段时间里用
户双击、网络层重试、或者超时后手工重发，都会发起第二个"同一次请求"——如果
服务端不去重，代价是重复扣费，并且可能真的把同一个任务创建两遍。

做法是让客户端带一个 ``request_id``：第一件事是抢占这个 id（主键插入），
抢到就执行，抢不到说明已有同 id 的请求在处理或已完成。完成时把整个响应体
存下来，重放时直接返回，用户看到的结果与第一次完全一致。
"""
from sqlalchemy import Column, DateTime, String, Text

from app.database import Base
from app.utils import utcnow

#: 单次请求允许的最长执行时间之外，还留出余量：超过这个时长仍停在
#: ``running`` 的行，视为进程崩溃留下的孤儿，允许后来者接管。
STALE_RUNNING_SECONDS = 600


class ChatRequest(Base):
    __tablename__ = "chat_requests"

    #: 客户端生成的请求 id。用主键做唯一约束，比"先查再插"少一次竞态窗口。
    id = Column(String(64), primary_key=True)

    session_id = Column(String(64), nullable=False, index=True)

    #: running / done / failed。只有 done 会重放响应体。
    status = Column(String(16), nullable=False, default="running")

    #: 用户消息的前若干字符，排查时能看出这条请求在干什么，不做业务用途。
    message_preview = Column(String(200), nullable=True)

    #: 完整响应体（AgentChatResponse 的 JSON），重放时原样返回。
    response_json = Column(Text, nullable=True)

    #: 失败原因摘要；status=failed 时才有值。
    error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)

    #: 每次状态流转都刷新，用来判定 running 是否已成孤儿。
    updated_at = Column(DateTime, default=utcnow, nullable=False)
