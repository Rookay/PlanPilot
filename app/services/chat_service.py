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

from sqlalchemy.orm import Session

from app.models.chat import ChatMessage


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

    return (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .delete(synchronize_session=False)
    )


def message_count(db: Session, session_id: str) -> int:
    """会话内的消息条数（测试与调试用）。"""

    return (
        db.query(ChatMessage).filter(ChatMessage.session_id == session_id).count()
    )
