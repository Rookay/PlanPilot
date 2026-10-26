# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：test_chat_history.py
"""短期对话历史的读写。

历史来自 ``chat_messages`` 表，按请求读取（见 ``app.services.chat_service``），
不再是进程内字典。这里锁住三件事：窗口取的是"最近 N 条"且时序为正、会话之间
互不串味、删除即生效。原先那份并发测试是针对内存字典加锁写的，状态没了，
测试对象也就没了——真正需要守住的不变量换成了这几个。
"""

from unittest import TestCase

from app.agent.memory import Memory
from app.services import chat_service
from tests.helpers import make_session


class ChatHistoryWindowTests(TestCase):
    def setUp(self):
        self.db = make_session()

    def test_messages_are_returned_in_chronological_order(self):
        for text in ("第一句", "第二句", "第三句"):
            chat_service.append_message(self.db, "s", "user", text)
        self.db.commit()

        history = chat_service.recent_messages(self.db, "s", limit=10)
        self.assertEqual(
            [item["content"] for item in history], ["第一句", "第二句", "第三句"]
        )

    def test_window_keeps_the_latest_messages(self):
        for index in range(30):
            chat_service.append_message(self.db, "s", "user", f"第{index}条")
        self.db.commit()

        history = chat_service.recent_messages(self.db, "s", limit=5)
        self.assertEqual(len(history), 5)
        self.assertEqual(history[0]["content"], "第25条")
        self.assertEqual(history[-1]["content"], "第29条")

    def test_non_positive_limit_returns_empty(self):
        chat_service.append_message(self.db, "s", "user", "在")
        self.db.commit()

        self.assertEqual(chat_service.recent_messages(self.db, "s", limit=0), [])

    def test_sessions_are_isolated(self):
        chat_service.append_message(self.db, "a", "user", "A 的话")
        chat_service.append_message(self.db, "b", "user", "B 的话")
        self.db.commit()

        self.assertEqual(
            [item["content"] for item in chat_service.recent_messages(self.db, "a")],
            ["A 的话"],
        )
        self.assertEqual(
            [item["content"] for item in chat_service.recent_messages(self.db, "b")],
            ["B 的话"],
        )

    def test_roles_are_preserved(self):
        chat_service.append_message(self.db, "s", "user", "问")
        chat_service.append_message(self.db, "s", "assistant", "答")
        self.db.commit()

        self.assertEqual(
            [item["role"] for item in chat_service.recent_messages(self.db, "s")],
            ["user", "assistant"],
        )


class ChatHistoryMutationTests(TestCase):
    def setUp(self):
        self.db = make_session()

    def test_clear_removes_only_target_session(self):
        for index in range(3):
            chat_service.append_message(self.db, "keep", "user", f"k{index}")
            chat_service.append_message(self.db, "drop", "user", f"d{index}")
        self.db.commit()

        deleted = chat_service.clear_messages(self.db, "drop")
        self.db.commit()

        self.assertEqual(deleted, 3)
        self.assertEqual(chat_service.message_count(self.db, "drop"), 0)
        self.assertEqual(chat_service.message_count(self.db, "keep"), 3)

    def test_append_does_not_commit_on_its_own(self):
        """事务边界留给 API 层。

        如果 append 内部偷偷 commit，一次失败的请求就会在表里留下半截对话，
        下一次请求再把这段没发生过的上下文喂回模型——排查起来会非常费劲。
        """

        chat_service.append_message(self.db, "s", "user", "不该留下")
        self.db.rollback()

        self.assertEqual(chat_service.message_count(self.db, "s"), 0)


class MemoryPreferenceTests(TestCase):
    """Memory 只剩长期偏好；短期历史已经下沉到 chat_messages。"""

    def setUp(self):
        self.db = make_session()
        self.memory = Memory()

    def test_defaults_are_readable(self):
        preferences = self.memory.get_preferences(self.db)
        self.assertIn("timezone", preferences)
        self.assertIn("workday_start", preferences)

    def test_preference_update_persists(self):
        self.memory.set_preference(self.db, "timezone", "Asia/Tokyo")
        self.assertEqual(
            self.memory.get_preferences(self.db)["timezone"], "Asia/Tokyo"
        )

    def test_short_term_state_is_gone(self):
        """回归：这些方法一旦被加回来，说明进程内会话状态又开始长了。"""

        for name in ("add", "recent", "hydrate", "has_session", "clear"):
            self.assertFalse(hasattr(self.memory, name), f"Memory.{name} 不应存在")
