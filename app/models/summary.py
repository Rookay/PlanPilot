# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：summary.py
"""会话摘要模型：``session_summaries`` 表。

层二（context）的落地点。它和 ``chat_messages`` 的分工是：

- ``chat_messages`` 是全量流水，只增不改，回答"当时到底说了什么"；
- ``session_summaries`` 是滚动摘要，一条会话一行，``covered_to_message_id``
  单调前进，回答"更早的对话里有哪些还必须记住的东西"。

为什么不把摘要也写进 ``chat_messages``：摘要每次折叠都会整段重写，混进流水后
"哪句是用户真说的、哪句是模型归纳的"就分不清了。排查幻觉时这个区分是刚需，
所以宁可多一张表。

``source`` 记录摘要由谁产生（model / deterministic），渲染时措辞不同：模型归纳
的五段摘要可以当成结论用；确定性压缩只是把原话揉短，不能当结论。
"""
from sqlalchemy import Column, DateTime, Integer, String, Text

from app.database import Base
from app.utils import utcnow


class SessionSummary(Base):
    __tablename__ = "session_summaries"

    id = Column(Integer, primary_key=True, index=True)

    #: 一个会话一条摘要——滚动覆盖，不做版本链。需要历史版本时查 chat_messages。
    session_id = Column(String(64), nullable=False, unique=True, index=True)

    #: JSON 文本。模型路径是五段结构；确定性路径是 {"digest": "..."}。
    content = Column(Text, nullable=False, default="")

    #: 已折进摘要的最后一条 chat_messages.id。查询窗口时只看 id 大于它的消息。
    covered_to_message_id = Column(Integer, nullable=False, default=0)

    #: 累计折叠条数，用于给用户展示"摘要覆盖了多少轮"。
    covered_count = Column(Integer, nullable=False, default=0)

    source = Column(String(20), nullable=False, default="model")

    #: 摘要自身的 token 估算，便于和预算对照（不参与计算，只用于排查）。
    token_estimate = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
