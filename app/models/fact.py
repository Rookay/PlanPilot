# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：fact.py
"""长期事实：``memory_facts`` 表。

层三的"事实区"。和 ``user_preferences`` 的区别是**谁说了算**：

- 偏好是用户显式设定的结构化配置（工作时段、午休、时区），字段固定，改就是改；
- 事实是从对话里**归纳**出来的零散结论（"导师姓王"、"每周三晚上有组会"），
  来源是模型推断，所以不能一出现就当真。

因此事实走候选制：第一次出现只记成 ``candidate``，同类事实再次出现把
``hit_count`` 加一，满两次才转 ``confirmed`` 并注入 system prompt。用户也可以
直接确认或否决——``rejected`` 之后不会被自动提取重新拉回候选，否则否决就没意义。
"""
from sqlalchemy import Column, DateTime, Integer, String, Text

from app.database import Base
from app.utils import utcnow

#: 状态常量。用字符串而不是枚举，SQLite 迁移时不需要改表结构。
STATUS_CANDIDATE = "candidate"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"

#: 出现多少次转正。2 是"至少被独立提到过两次"的最小值；1 就等于相信一次推断。
CONFIRM_HIT_THRESHOLD = 2

CATEGORIES = ("constraint", "decision", "entity", "preference", "other")


class MemoryFact(Base):
    __tablename__ = "memory_facts"

    id = Column(Integer, primary_key=True, index=True)

    #: 归一化文本的哈希。用它做去重键：同一句话换个标点不该算两条事实。
    fingerprint = Column(String(40), nullable=False, unique=True, index=True)

    fact = Column(Text, nullable=False)

    category = Column(String(20), nullable=False, default="other")

    status = Column(String(20), nullable=False, default=STATUS_CANDIDATE, index=True)

    #: 被独立提及的次数。转正靠它，不靠时间——时间只能说明"旧"，不能说明"对"。
    hit_count = Column(Integer, nullable=False, default=1)

    #: 首次出现的会话，便于回溯"这条事实是哪次聊出来的"。
    session_id = Column(String(64), nullable=True, index=True)

    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    last_seen_at = Column(DateTime, default=utcnow, nullable=False)
