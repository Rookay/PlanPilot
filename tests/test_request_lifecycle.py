"""请求生命周期：SQLite 并发参数、幂等、单次请求时长上限。

这三样是同一个问题的三个面——一次对话请求可能跑十几轮工具调用、几十秒。
如果没有时长上限、不能取消、不能去重，那么"双击一次发送"或"超时后重发"就会
真的把同一个任务创建两遍；而"两个人同时点发送"会撞上 SQLite 的写锁。
"""

import os
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.confirmation import PendingActionStore
from app.agent.loop import AgentLoop, AgentResult
from app.agent.memory import Memory
from app.api import agent as agent_api
from app.database import configure_sqlite_connection, get_db
from app.llm.mock import MockProvider
from app.models.chat import ChatMessage
from app.models.chat_request import STALE_RUNNING_SECONDS, ChatRequest
from app.services import chat_request_service as crs
from app.skills.planner_skills import build_default_skills
from app.tools.defaults import build_default_registry
from app.utils import utcnow
from tests.helpers import make_session


class SQLitePragmaTests(TestCase):
    """并发相关的 pragma 必须真的生效，不能只写在注释里。"""

    def test_connection_gets_wal_and_busy_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "probe.db")
            connection = sqlite3.connect(path)
            try:
                configure_sqlite_connection(connection)

                # rollback journal 下写事务会挡住读，这个应用的长事务等不起。
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                    "wal",
                )
                # sqlite3 默认 5 秒，并发提交时不够。
                self.assertEqual(
                    connection.execute("PRAGMA busy_timeout").fetchone()[0], 15000
                )
                self.assertEqual(
                    connection.execute("PRAGMA foreign_keys").fetchone()[0], 1
                )
            finally:
                connection.close()


class ReservationTests(TestCase):
    """request_id 抢占的各种状态迁移。"""

    def setUp(self):
        self.db = make_session()

    def tearDown(self):
        self.db.close()

    def test_first_reserve_runs_and_second_is_rejected(self):
        self.assertEqual(crs.reserve(self.db, "r1", "s1", "你好").state, "new")
        # 同一个 id 再来：上一次还在跑，必须挡住，否则就是第二条并行的模型调用。
        self.assertEqual(crs.reserve(self.db, "r1", "s1", "你好").state, "running")

    def test_finished_request_replays_stored_response(self):
        crs.reserve(self.db, "r1", "s1", "你好")
        crs.finish(self.db, "r1", {"reply": "已经排好了", "run_id": "run-1"})
        self.db.commit()

        reservation = crs.reserve(self.db, "r1", "s1", "你好")

        self.assertEqual(reservation.state, "done")
        self.assertEqual(reservation.response["reply"], "已经排好了")

    def test_failed_request_can_be_retried_with_same_id(self):
        crs.reserve(self.db, "r1", "s1", "你好")
        crs.fail(self.db, "r1", "LLMTransientError: timeout")
        self.db.commit()

        # 失败没有可重放的响应，占着 id 只会让用户的"重试"永远拿到 409。
        self.assertEqual(crs.reserve(self.db, "r1", "s1", "你好").state, "new")

    def test_stale_running_is_taken_over(self):
        crs.reserve(self.db, "r1", "s1", "你好")
        row = self.db.get(ChatRequest, "r1")
        row.updated_at = utcnow() - timedelta(seconds=STALE_RUNNING_SECONDS + 5)
        self.db.commit()

        # 进程崩在中间时状态会一直停在 running，超时后必须允许接管。
        self.assertEqual(crs.reserve(self.db, "r1", "s1", "你好").state, "new")

    def test_fresh_running_is_not_taken_over(self):
        crs.reserve(self.db, "r1", "s1", "你好")
        row = self.db.get(ChatRequest, "r1")
        row.updated_at = utcnow() - timedelta(seconds=10)
        self.db.commit()

        self.assertEqual(crs.reserve(self.db, "r1", "s1", "你好").state, "running")

    def test_done_without_payload_is_rerun(self):
        crs.reserve(self.db, "r1", "s1", "你好")
        row = self.db.get(ChatRequest, "r1")
        row.status = "done"
        row.response_json = "{不是合法 JSON"
        self.db.commit()

        # 缓存坏了宁可多跑一次，也不能把一个前端解析不了的响应当结果。
        self.assertEqual(crs.reserve(self.db, "r1", "s1", "你好").state, "new")

    def test_clear_for_session_removes_rows(self):
        crs.reserve(self.db, "r1", "s1", "你好")
        crs.reserve(self.db, "r2", "s2", "另一个会话")

        removed = crs.clear_for_session(self.db, "s1")
        self.db.commit()

        self.assertEqual(removed, 1)
        self.assertIsNone(self.db.get(ChatRequest, "r1"))
        self.assertIsNotNone(self.db.get(ChatRequest, "r2"))


class _FakeLoop:
    """只为 API 层测试提供 _chat_once 会读的那几个属性。"""

    skill_registry = None
    last_run_provider = "MockProvider"
    llm = MockProvider()


class _FakeAgent:
    def __init__(self):
        self.loop = _FakeLoop()
        self.calls = []

    def handle(self, db, message, session_id, confirm_action_id=None, decision=None):
        self.calls.append(message)
        return AgentResult(reply=f"收到：{message}", session_id=session_id)


class ChatIdempotencyApiTests(TestCase):
    """同一个 request_id 第二次请求，不能产生第二次执行。"""

    def setUp(self):
        self.time_patcher = patch(
            "app.services.time_policy.utcnow",
            return_value=datetime(2026, 9, 15),
        )
        self.time_patcher.start()
        self.db = make_session()
        self.fake = _FakeAgent()
        self.agent_patcher = patch.object(agent_api, "agent", self.fake)
        self.agent_patcher.start()

        application = FastAPI()
        application.include_router(agent_api.router)

        def _db_override():
            yield self.db

        application.dependency_overrides[get_db] = _db_override
        self.client = TestClient(application)

    def tearDown(self):
        self.client.close()
        self.agent_patcher.stop()
        self.time_patcher.stop()
        self.db.close()

    def _post(self, **payload):
        return self.client.post("/api/agent/chat", json=payload)

    def test_duplicate_request_id_replays_without_executing_again(self):
        first = self._post(message="看看任务", session_id="s1", request_id="req-1")
        second = self._post(message="看看任务", session_id="s1", request_id="req-1")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["reply"], second.json()["reply"])
        # 只跑了一次：模型调用与扣费都不会翻倍。
        self.assertEqual(len(self.fake.calls), 1)

    def test_replay_does_not_duplicate_messages(self):
        self._post(message="看看任务", session_id="s1", request_id="req-1")
        self._post(message="看看任务", session_id="s1", request_id="req-1")

        # 一问一答两条；重放不该再写一对。
        self.assertEqual(
            self.db.query(ChatMessage)
            .filter(ChatMessage.session_id == "s1")
            .count(),
            2,
        )

    def test_inflight_request_returns_409(self):
        # 模拟"上一次还在跑"：先占住 id。
        crs.reserve(self.db, "req-1", "s1", "看看任务")

        response = self._post(message="看看任务", session_id="s1", request_id="req-1")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.fake.calls, [])

    def test_missing_request_id_always_executes(self):
        # 旧客户端不带 request_id，行为不变：每次都执行。
        self._post(message="看看任务", session_id="s1")
        self._post(message="看看任务", session_id="s1")

        self.assertEqual(len(self.fake.calls), 2)

    def test_failure_marks_request_failed_not_stuck_running(self):
        def boom(*_args, **_kwargs):
            raise RuntimeError("模型炸了")

        self.fake.handle = boom
        with self.assertRaises(RuntimeError):
            self._post(message="看看任务", session_id="s1", request_id="req-1")

        row = self.db.get(ChatRequest, "req-1")
        self.assertEqual(row.status, "failed")
        self.assertIn("模型炸了", row.error)


class _SlowProvider(MockProvider):
    """每次调用都睡一会儿，用来把循环推到时长上限。"""

    def __init__(self, delay, script=None):
        super().__init__(script=script)
        self.delay = delay

    def chat(self, messages, tools=None):
        time.sleep(self.delay)
        return super().chat(messages, tools)


class RunDeadlineTests(TestCase):
    """12 轮 × 单次超时只约束"一轮"，不约束"一次请求"。"""

    def setUp(self):
        self.time_patcher = patch(
            "app.services.time_policy.utcnow",
            return_value=datetime(2026, 9, 15),
        )
        self.time_patcher.start()
        self.db = make_session()

    def tearDown(self):
        self.time_patcher.stop()
        self.db.close()

    def _loop(self, provider):
        return AgentLoop(
            llm=provider,
            registry=build_default_registry(),
            memory=Memory(),
            confirmation_store=PendingActionStore(),
            skill_registry=build_default_skills(),
        )

    def test_deadline_stops_run_and_keeps_partial_results(self):
        script = [
            {"tool_calls": [{"name": "get_current_time", "arguments": {}}]}
            for _ in range(12)
        ]
        loop = self._loop(_SlowProvider(0.05, script=script))

        with patch.object(AgentLoop, "RUN_DEADLINE_SECONDS", 0.1):
            started = time.monotonic()
            result = loop.run(self.db, "现在几点", "s1")
            elapsed = time.monotonic() - started

        # 不抛异常，带着已完成的部分收尾。
        self.assertIn("上限", result.reply)
        self.assertLess(elapsed, 3.0)
        # 已经执行过的工具调用不能被丢弃：它们是这次请求的全部产出。
        self.assertTrue(result.tool_calls)

        steps = [step for step in result.workflow_steps if step.get("kind") == "deadline"]
        self.assertEqual(len(steps), 1)
        # warning 而不是 failed：改了的东西没有回滚，只是没做完。
        self.assertEqual(steps[0]["status"], "warning")

    def test_fast_run_has_no_deadline_step(self):
        loop = self._loop(MockProvider(script=[{"text": "好的"}]))

        result = loop.run(self.db, "你好", "s1")

        self.assertEqual(
            [step for step in result.workflow_steps if step.get("kind") == "deadline"],
            [],
        )
