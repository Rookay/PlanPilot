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

还有一个容易漏掉的状态 ``superseded``：同一件事的值被改了（"截止 10-30"
改成 "截止 10-29"）。指纹是全文哈希，值一变就是新指纹，于是新旧两条并存，
prompt 里同时给模型两个互相矛盾的截止日——它只能猜。所以写入新值时要把
同主题的旧值标成 ``superseded``，让它退出注入但不删除（还看得出改过什么）。
"""
from sqlalchemy import Column, DateTime, Integer, String, Text

from app.database import Base
from app.utils import utcnow

#: 状态常量。用字符串而不是枚举，SQLite 迁移时不需要改表结构。
STATUS_CANDIDATE = "candidate"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
#: 同主题有了更新的值，这条已过时。保留以便追溯，但不注入 prompt。
STATUS_SUPERSEDED = "superseded"

#: 出现多少次转正。2 是"至少被独立提到过两次"的最小值；1 就等于相信一次推断。
CONFIRM_HIT_THRESHOLD = 2

CATEGORIES = ("constraint", "decision", "entity", "preference", "other")


class MemoryFact(Base):
    __tablename__ = "memory_facts"

    id = Column(Integer, primary_key=True, index=True)

    #: 归一化文本的哈希。用它做去重键：同一句话换个标点不该算两条事实。
    fingerprint = Column(String(40), nullable=False, unique=True, index=True)

    #: 主题键：这条事实说的是"哪件事"，不含具体值。
    #: 例如 "季度总结截止 10-30" 的主题是 "季度总结截止日期"。同一主题出现
    #: 新值时，旧值退出注入（见 ``STATUS_SUPERSEDED``）。模型没给时留空，
    #: 由 fact_service 用"剥离数字后的骨架"兜底。
    subject = Column(String(120), nullable=True, index=True)

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
