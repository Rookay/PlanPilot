# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：chat_service.py
"""对话历史服务：``chat_messages`` 表的唯一读写入口。

这里取代了原先放在 ``Memory`` 里的进程内会话字典。换掉它的理由不是"落库更
正规"，而是那份字典把会话状态绑死在了单个进程上：

- uvicorn 开多 worker 时，同一会话的第二个请求可能落到另一个进程，它的
  ``_sessions`` 是空的，模型于是看不到上一轮说了什么；
- 进程重启即失忆，而数据库里的记录还在，两边的"记忆"从此不一致；
- 为了补这个洞才长出了 ``has_session`` / ``hydrate`` 这一对方法——先问
  "我有没有这个会话"，没有再去表里捞。本质上是把缓存一致性问题搬进了
  业务代码，还得靠加锁维护。

现在改成每次请求按需从表里读窗口：没有进程内状态，也就不存在同步问题。
写入仍然走同一个入口，但事务边界留在 API 层——它才知道一次请求从哪里开始、
到哪里提交。
"""

from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.chat import ChatMessage
from app.models.chat_tool_call import ChatToolCall


def recent_messages(db: Session, session_id: str, limit: int = 20) -> List[dict]:
    """取最近 ``limit`` 条消息，按时间正序返回（最早的在前）。

    LLM 的 messages 数组要求时序，所以这里倒序取窗口、返回前再翻正：正序查
    再取末尾 N 条在语义上等价，但要先把整段会话读出来，会话一长就白扫一遍。
    """

    if limit <= 0:
        return []

    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.id.desc())
        .limit(limit)
        .all()
    )
    rows.reverse()
    return [{"role": row.role, "content": row.content} for row in rows]


def window_messages(db: Session, session_id: str, limit: int = 60) -> List[dict]:
    """取最近 ``limit`` 条消息，**带 id**，按时间正序返回。

    和 :func:`recent_messages` 的唯一区别是多了 ``id``。摘要层需要它来判断
    "这条消息是不是已经被折进摘要了"——``covered_to_message_id`` 靠 id 比较，
    用条数或时间都不行：条数会被并发写入打乱，时间在同一秒内不可靠。
    """

    if limit <= 0:
        return []

    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.id.desc())
        .limit(limit)
        .all()
    )
    rows.reverse()
    return [
        {"id": row.id, "role": row.role, "content": row.content} for row in rows
    ]


def append_message(
    db: Session,
    session_id: str,
    role: str,
    content: str,
    agent_run_id: Optional[str] = None,
) -> ChatMessage:
    """追加一条消息。

    **不 commit**：一次对话要同时写 chat_messages 和 agent_runs，两者必须
    落在同一个事务里，否则会出现"有回复记录但没有运行轨迹"的半截状态。
    """

    message = ChatMessage(
        session_id=session_id,
        role=role,
        content=content,
        agent_run_id=agent_run_id,
    )
    db.add(message)
    return message


def clear_messages(db: Session, session_id: str) -> int:
    """删除某个会话的全部消息，返回删除条数。**不 commit**。"""

    # 轨迹跟着消息一起删。留着的后果不是"占点空间"，而是清空会话后回看还能
    # 看到上一段对话调过哪些工具，等于没清干净。
    db.query(ChatToolCall).filter(
        ChatToolCall.session_id == session_id
    ).delete(synchronize_session=False)

    return (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .delete(synchronize_session=False)
    )


def append_tool_calls(
    db: Session,
    session_id: str,
    agent_run_id: Optional[str],
    records: List[dict],
) -> int:
    """把一轮请求的工具调用轨迹落库，返回写入条数。**不 commit**。

    和 :func:`append_message` 一样，事务边界留在 API 层：轨迹、消息、运行记录
    必须同生同死，否则会出现"有轨迹但查不到对应回复"的残片。
    """

    for record in records or []:
        db.add(
            ChatToolCall(
                session_id=session_id,
                agent_run_id=agent_run_id,
                turn_index=int(record.get("turn_index") or 0),
                tool_call_id=str(record.get("tool_call_id") or ""),
                name=str(record.get("name") or ""),
                arguments=str(record.get("arguments") or ""),
                success=bool(record.get("success")),
                summary=record.get("summary"),
                error=record.get("error"),
                result=record.get("result"),
                result_chars=int(record.get("result_chars") or 0),
            )
        )
    return len(records or [])


def recent_tool_calls(
    db: Session,
    session_id: str,
    agent_run_id: Optional[str] = None,
    limit: int = 200,
) -> List[ChatToolCall]:
    """读某个会话（或某次运行）的工具调用轨迹，按执行顺序返回。

    轨迹**不进 prompt**：它是审计与回放用的。混进上下文只会挤占预算，还会让
    模型把历史里早已过时的工具返回值当成当前事实。
    """

    if limit <= 0:
        return []

    query = db.query(ChatToolCall).filter(ChatToolCall.session_id == session_id)
    if agent_run_id:
        query = query.filter(ChatToolCall.agent_run_id == agent_run_id)

    rows = query.order_by(ChatToolCall.id.desc()).limit(limit).all()
    rows.reverse()
    return rows


def message_count(db: Session, session_id: str) -> int:
    """会话内的消息条数（测试与调试用）。"""

    return (
        db.query(ChatMessage).filter(ChatMessage.session_id == session_id).count()
    )


def list_sessions(db: Session, limit: int = 50) -> List[dict]:
    """按最近活跃排序的会话概览，供前端会话列表使用。

    前端原先只认 localStorage 里那一个 ``session_id``，换 origin 或清缓存就
    "看不到历史"——数据一直在表里，只是没有入口。这里补的是那个入口。

    ``title`` 取首条 user 消息：助手回复通常以"已为你…"开头，第一句人话更能
    代表这个会话在聊什么。``preview`` 取最后一条消息，用来显示讲到哪儿了。

    三次查询而不是 N+1：聚合一次拿会话，再各取首条 user / 末条消息的内容。
    """

    aggregates = (
        db.query(
            ChatMessage.session_id.label("session_id"),
            func.count(ChatMessage.id).label("message_count"),
            func.max(ChatMessage.created_at).label("last_at"),
            func.max(ChatMessage.id).label("last_id"),
        )
        .group_by(ChatMessage.session_id)
        .order_by(func.max(ChatMessage.created_at).desc())
        .limit(limit)
        .all()
    )
    if not aggregates:
        return []

    session_ids = [row.session_id for row in aggregates]

    # 每个会话第一条 user 消息的 id（可能没有 user 消息，比如 legacy）。
    first_user_ids = dict(
        db.query(ChatMessage.session_id, func.min(ChatMessage.id))
        .filter(
            ChatMessage.session_id.in_(session_ids),
            ChatMessage.role == "user",
        )
        .group_by(ChatMessage.session_id)
        .all()
    )

    wanted_ids = list(first_user_ids.values()) + [row.last_id for row in aggregates]
    contents = {
        message.id: message.content
        for message in db.query(ChatMessage)
        .filter(ChatMessage.id.in_(wanted_ids))
        .all()
    }

    sessions: List[dict] = []
    for row in aggregates:
        title = contents.get(first_user_ids.get(row.session_id), "")
        preview = contents.get(row.last_id, "")
        sessions.append(
            {
                "session_id": row.session_id,
                "message_count": row.message_count,
                "last_message_at": row.last_at,
                "title": (title or "（无用户消息）").strip().replace("\n", " ")[:40],
                "preview": (preview or "").strip().replace("\n", " ")[:60],
            }
        )
    return sessions
