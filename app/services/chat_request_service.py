# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：chat_request_service.py
"""对话请求的幂等控制。

为什么必须在这里单独提交一次事务：抢占 ``request_id`` 必须**先落盘**才能被
另一个进程或线程看见。如果把它留在请求末尾那次大提交里，两个并发请求会各自
在未提交的事务中插入同一个主键，谁也看不见谁，直到最后一方 commit 才报错——
那时模型和工具都已经跑完，钱已经花了，去重就失去意义。

因此这里的 commit 是刻意为之，不是"忘了合并事务"：抢占是一次很短的写事务，
之后的长执行不持锁。
"""
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.chat_request import STALE_RUNNING_SECONDS, ChatRequest
from app.utils import utcnow


@dataclass
class Reservation:
    """一次抢占的结果。

    - ``new``：抢到了，调用方负责执行并在结束时 finish / fail。
    - ``running``：同 id 的请求正在执行，调用方应回 409。
    - ``done``：同 id 已成功执行完，``response`` 是当时的响应体，直接重放。
    """

    state: str
    response: Optional[dict] = None


def _response_of(row: ChatRequest) -> Optional[dict]:
    if not row.response_json:
        return None
    try:
        return json.loads(row.response_json)
    except (TypeError, ValueError):
        # 存坏了就当没有缓存：宁可多跑一次，也不要返回一个前端解析不了的响应。
        return None


def _is_stale(row: ChatRequest) -> bool:
    if row.status != "running":
        return False
    updated = row.updated_at or row.created_at
    if updated is None:
        return True
    return utcnow() - updated > timedelta(seconds=STALE_RUNNING_SECONDS)


def reserve(
    db: Session,
    request_id: str,
    session_id: str,
    message: str = "",
) -> Reservation:
    """抢占 request_id。返回 ``new`` 才表示本次请求应该真正执行。"""

    existing = db.get(ChatRequest, request_id)
    if existing is not None:
        if existing.status == "done":
            payload = _response_of(existing)
            if payload is not None:
                return Reservation("done", payload)
            # 状态是 done 但响应体丢了：当作没跑过，重新执行更安全。
            existing.status = "running"
            existing.updated_at = utcnow()
            db.commit()
            return Reservation("new")
        if existing.status == "failed":
            # 上次失败了，同一 id 允许重试：失败既没有可重放的响应，也不该让这个
            # id 永久占用（用户点"重试"本来就该真的再跑一次）。
            existing.status = "running"
            existing.updated_at = utcnow()
            existing.error = None
            existing.message_preview = (message or "")[:200]
            db.commit()
            return Reservation("new")
        if existing.status == "running" and _is_stale(existing):
            # 进程崩在中间，状态会一直停在 running。超时后允许接管，否则这个
            # request_id 就永久废了，用户重发也永远拿到 409。
            existing.status = "running"
            existing.updated_at = utcnow()
            existing.message_preview = (message or "")[:200]
            db.commit()
            return Reservation("new")
        return Reservation("running")

    db.add(
        ChatRequest(
            id=request_id,
            session_id=session_id,
            status="running",
            message_preview=(message or "")[:200],
            updated_at=utcnow(),
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # 两个线程同时过了上面的 db.get：主键冲突说明对方赢了，按已存在处理。
        db.rollback()
        row = db.get(ChatRequest, request_id)
        if row is not None and row.status == "done":
            payload = _response_of(row)
            if payload is not None:
                return Reservation("done", payload)
        return Reservation("running")

    return Reservation("new")


def finish(db: Session, request_id: str, response: dict) -> None:
    """记录成功结果。响应体落库，重复请求会原样重放这一份。"""

    row = db.get(ChatRequest, request_id)
    if row is None:
        return
    row.status = "done"
    row.response_json = json.dumps(response, ensure_ascii=False, default=str)
    row.error = None
    row.updated_at = utcnow()


def fail(db: Session, request_id: str, error: str) -> None:
    """记录失败。失败不缓存响应体，但也不再允许重放。"""

    row = db.get(ChatRequest, request_id)
    if row is None:
        return
    row.status = "failed"
    row.error = (error or "")[:1000]
    row.updated_at = utcnow()


def get(db: Session, request_id: str) -> Optional[ChatRequest]:
    return db.get(ChatRequest, request_id)


def clear_for_session(db: Session, session_id: str) -> int:
    """删除会话时一并清理，避免留下指向已删会话的死记录。"""

    return (
        db.query(ChatRequest)
        .filter(ChatRequest.session_id == session_id)
        .delete(synchronize_session=False)
    )
