# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：memory.py
"""Memory：长期记忆访问器。

只保留**跨会话有效**的内容——用户偏好。短期对话历史不在这里，它在
``chat_messages`` 表里，由 ``app.services.chat_service`` 按请求读取。

为什么把两者分开：短期历史的生命周期是"一次请求的一段窗口"，本质上是待
查询的数据，缓存它只会带来一致性问题；而偏好是"所有请求共享的偏置"，读一次
就能用完，放哪儿都不影响正确性。
"""

from sqlalchemy.orm import Session

from app.schemas.preference import PreferenceUpdate
from app.services import preference_service


class Memory:
    """长期记忆（用户偏好）。"""

    def get_preferences(self, db: Session) -> dict:
        return preference_service.get_preferences(db).model_dump()

    def set_preference(self, db: Session, key: str, value: str) -> None:
        preference_service.update_preferences(db, PreferenceUpdate(**{key: value}))
