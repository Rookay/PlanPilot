# -*- coding: UTF-8 -*-
"""确认操作的一次性消费。

待确认操作落库之后可以跨进程共享，代价是"两个进程都能对它执行一遍"：
用户双击确认、两个标签页都点了、或者多 worker 各收到一份，都会读到同一份
``tool_calls`` 各跑一次。

``chat_requests`` 那层幂等挡不住这个：它认的是 ``request_id``，而两次点击
各自生成新的 id（见 ``frontend/js/chat.js`` 的确认请求），在服务端看来是两次
独立的请求。要挡的是**资源**（这份待确认操作）被消费几次，不是请求重发。

所以 ``claim`` 用条件更新抢占：``UPDATE ... WHERE id=? AND status='pending'``，
只有一条语句能改到行。这里的测试覆盖抢占、隔离、退回、超时接管，以及最关键的
"并发确认只执行一次"。
"""

import os
import tempfile
import threading
import time
from datetime import timedelta
from unittest import TestCase

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401  建表前必须先导入所有模型
from app.agent.agent import Agent
from app.agent.confirmation import PendingActionStore
from app.agent.memory import Memory
from app.database import Base, configure_sqlite_connection
from app.llm.mock import MockProvider
from app.models.pending_action import STATUS_PENDING, STATUS_TAKEN, PendingAction
from app.models.task import Task
from app.skills.planner_skills import build_default_skills
from app.tools.defaults import build_default_registry
from app.utils import utcnow


def _shared_factory():
    """一个引擎、多个 Session 共享同一份内存库，用来模拟两个请求。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, future=True)


def _tool_calls():
    return [
        {"name": "create_task", "args": {"title": "开会"}},
        {"name": "create_task", "args": {"title": "写周报"}},
    ]


class PendingActionClaimTests(TestCase):
    """抢占语义。用两个 Session 模拟两个并发请求。"""

    def setUp(self):
        self.Session = _shared_factory()
        self.store = PendingActionStore()
        self.writer = self.Session()
        self.action_id = self.store.create(
            self.writer,
            tool_calls=_tool_calls(),
            summary="新增 2 个任务",
            session_id="s1",
        )

    def tearDown(self):
        self.writer.close()

    def test_claim_returns_view_and_marks_taken(self):
        view = self.store.claim(self.writer, self.action_id, session_id="s1")

        self.assertIsNotNone(view)
        self.assertEqual(view.tool_calls, _tool_calls())
        self.assertEqual(
            self.writer.get(PendingAction, self.action_id).status, STATUS_TAKEN
        )

    def test_second_claim_is_rejected(self):
        self.assertIsNotNone(self.store.claim(self.writer, self.action_id, "s1"))

        # 另一个请求（独立 Session）拿不到：这是双击确认的第二种点法。
        other = self.Session()
        try:
            self.assertIsNone(self.store.claim(other, self.action_id, "s1"))
        finally:
            other.close()

    def test_claim_does_not_consume_for_other_session(self):
        other = self.Session()
        try:
            self.assertIsNone(self.store.claim(other, self.action_id, "other-session"))
        finally:
            other.close()

        # 关键：被拒绝的越权请求不能把状态改成 taken，否则真正的会话再也领不到。
        self.assertEqual(
            self.writer.get(PendingAction, self.action_id).status, STATUS_PENDING
        )
        self.assertIsNotNone(self.store.claim(self.writer, self.action_id, "s1"))

    def test_release_allows_retry(self):
        self.store.claim(self.writer, self.action_id, "s1")
        self.store.release(self.writer, self.action_id)

        # 执行失败退回 pending：用户可以重新点一次确认。
        self.assertEqual(
            self.writer.get(PendingAction, self.action_id).status, STATUS_PENDING
        )
        self.assertIsNotNone(self.store.claim(self.writer, self.action_id, "s1"))

    def test_stale_taken_can_be_taken_over(self):
        self.store.claim(self.writer, self.action_id, "s1")
        record = self.writer.get(PendingAction, self.action_id)
        record.taken_at = utcnow() - timedelta(
            seconds=self.store.claim_timeout_seconds + 5
        )
        self.writer.commit()

        # 进程崩在中间时没人会来释放，必须靠超时接管，否则这份操作永久卡死。
        self.assertIsNotNone(self.store.claim(self.writer, self.action_id, "s1"))

    def test_fresh_taken_is_not_taken_over(self):
        self.store.claim(self.writer, self.action_id, "s1")
        record = self.writer.get(PendingAction, self.action_id)
        record.taken_at = utcnow() - timedelta(seconds=5)
        self.writer.commit()

        self.assertIsNone(self.store.claim(self.writer, self.action_id, "s1"))

    def test_get_does_not_consume(self):
        # 前端展示"待确认"用的是 get，它不能有副作用。
        self.assertIsNotNone(self.store.get(self.writer, self.action_id, "s1"))
        self.assertIsNotNone(self.store.get(self.writer, self.action_id, "s1"))
        self.assertIsNotNone(self.store.claim(self.writer, self.action_id, "s1"))

    def test_claim_returns_none_after_ttl(self):
        self.store.ttl_seconds = 0
        self.assertIsNone(self.store.claim(self.writer, self.action_id, "s1"))
        self.assertIsNone(self.writer.get(PendingAction, self.action_id))


class ConcurrentConfirmTests(TestCase):
    """真实文件库 + 多线程：并发确认同一份操作，工具只能执行一遍。

    必须用文件库：``StaticPool`` 的内存库所有线程共享同一个连接，
    连接内部自行串行，测不出两个进程各自读到 pending 的窗口。
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        path = os.path.join(self._tmpdir.name, "confirm.db")
        self.engine = create_engine(
            f"sqlite:///{path}",
            connect_args={"check_same_thread": False, "timeout": 15},
            pool_size=5,
            max_overflow=10,
            pool_timeout=15,
            future=True,
        )

        @event.listens_for(self.engine, "connect")
        def _apply_pragma(connection, _record):
            configure_sqlite_connection(connection)

        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, future=True)

        skills = build_default_skills()
        self.registry = build_default_registry(skills)
        self.agent = Agent(
            llm=MockProvider(),
            registry=self.registry,
            memory=Memory(),
            confirmation_store=PendingActionStore(),
            skill_registry=skills,
        )
        self.executed = []

    def tearDown(self):
        self.engine.dispose()
        self._tmpdir.cleanup()

    def _slow_execute(self):
        """在工具执行处加延迟，把"已领取未落地"的窗口撑到必然重叠。"""

        real_execute = self.registry.execute

        def execute(name, args, session):
            self.executed.append(name)
            time.sleep(0.2)
            return real_execute(name, args, session)

        self.registry.execute = execute

    def test_concurrent_confirm_executes_tools_once(self):
        self._slow_execute()
        db = self.Session()
        action_id = self.agent.confirmation_store.create(
            db,
            tool_calls=_tool_calls(),
            summary="新增 2 个任务",
            session_id="s1",
        )
        db.close()

        barrier = threading.Barrier(2)
        replies = []
        errors = []

        def confirm():
            session = self.Session()
            try:
                barrier.wait(timeout=10)
                result = self.agent.handle(
                    session, "", "s1", confirm_action_id=action_id, decision="accept"
                )
                replies.append(result.reply)
            except BaseException as exc:  # noqa: BLE001  线程异常要带回主线程
                errors.append(exc)
            finally:
                session.close()

        threads = [threading.Thread(target=confirm) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])

        db = self.Session()
        try:
            titles = sorted(task.title for task in db.query(Task).all())
        finally:
            db.close()

        # 2 个任务创建一遍 = 2 条；重复执行会变成 4 条、4 次工具调用。
        self.assertEqual(titles, ["写周报", "开会"])
        self.assertEqual(len(self.executed), 2)
        self.assertEqual(len([r for r in replies if r.startswith("已执行")]), 1)
        self.assertEqual(len([r for r in replies if "已经被另一次点击处理过" in r]), 1)
