# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：summary_service.py
"""``session_summaries`` 表的唯一读写入口。

不 commit：摘要是在构建 prompt 时顺手写的，和本轮对话共享同一个事务。这样
"摘要前进了但对话没落库"这种半截状态不会出现——请求失败时两者一起回滚，
下次重来一遍折叠即可，代价只是多调一次模型。

这条规则和 ``chat_service`` 保持一致，事务边界统一留在 API 层。
"""

from typing import Optional

from sqlalchemy.orm import Session

from app.agent.compaction import estimate_tokens
from app.models.summary import SessionSummary


def get_summary(db: Session, session_id: str) -> Optional[SessionSummary]:
    return (
        db.query(SessionSummary)
        .filter(SessionSummary.session_id == session_id)
        .one_or_none()
    )


def save_summary(
    db: Session,
    session_id: str,
    *,
    content: str,
    covered_to_message_id: int,
    covered_count: int,
    source: str,
) -> SessionSummary:
    """写入或覆盖摘要。**不 commit**。

    ``covered_to_message_id`` 只允许前进：并发请求可能各自算出一次折叠，后写的
    如果范围更小会把摘要改回旧状态，导致同一段对话被反复折叠、token 白烧。
    """

    record = get_summary(db, session_id)
    if record is None:
        # 显式给 0：Column 的 default 只在 INSERT 时生效，对象刚建出来时属性还是
        # None，直接拿它比大小会 TypeError。
        record = SessionSummary(
            session_id=session_id, covered_to_message_id=0, covered_count=0
        )
        db.add(record)
        # 生产 Session 是 autoflush=False，不 flush 的话同一事务内的下一次
        # get_summary 看不到这条记录，会再建一条同 session_id 的，撞唯一约束。
        db.flush()

    if covered_to_message_id < (record.covered_to_message_id or 0):
        return record

    record.content = content
    record.covered_to_message_id = covered_to_message_id
    record.covered_count = covered_count
    record.source = source
    record.token_estimate = estimate_tokens(content)
    return record


def clear_summary(db: Session, session_id: str) -> int:
    """删除摘要，返回删除条数。**不 commit**。"""

    return (
        db.query(SessionSummary)
        .filter(SessionSummary.session_id == session_id)
        .delete(synchronize_session=False)
    )
