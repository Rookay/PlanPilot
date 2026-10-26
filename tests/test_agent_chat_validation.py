# -*- coding: UTF-8 -*-
"""``/api/agent/chat`` 入参校验测试。

保护对象：``AgentChatRequest`` schema 的字段限制（message 长度 / session_id 字符集
/ decision 取值），以及 FastAPI 层把这些 Pydantic 校验转成 422 的行为。

历史教训：这些字段限制早就在 schema 里写过，但没有任何测试守住——一次
清理或重构就可能把它们悄悄丢掉。
"""

import warnings
from unittest import TestCase
from unittest.mock import patch

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated",
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import agent
from app.schemas.agent import AgentChatRequest


class AgentChatRequestSchemaTests(TestCase):
    """Pydantic 层校验。直接构造对象，应被 ValidationError 拒绝或放行。"""

    # ---- message 长度 ----

    def test_message_within_limit_is_accepted(self):
        msg = "x" * 20000
        req = AgentChatRequest(message=msg)
        self.assertEqual(len(req.message), 20000)

    def test_message_exceeding_limit_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            AgentChatRequest(message="x" * 20001)
        self.assertIn("at most 20000", str(ctx.exception))

    # ---- message 内容 ----

    def test_empty_message_without_confirm_is_rejected(self):
        with self.assertRaises(ValidationError):
            AgentChatRequest(message="")
        with self.assertRaises(ValidationError):
            AgentChatRequest(message="   \n  \t  ")

    def test_empty_message_with_confirm_is_accepted(self):
        # confirmation 回放路径不需要 message，只要 decision 在
        req = AgentChatRequest(
            message="",
            confirm_action_id="abc123",
            decision="accept",
        )
        self.assertEqual(req.message, "")

    # ---- session_id ----

    def test_session_id_default_characters_allowed(self):
        for valid in ["abc", "session-1", "abc_123", "AaBb_Cc-09"]:
            req = AgentChatRequest(message="hi", session_id=valid)
            self.assertEqual(req.session_id, valid)

    def test_session_id_rejects_special_characters(self):
        for bad in ["a b", "session/1", "abc.def", "session;1", "abc\nd"]:
            with self.assertRaises(ValidationError):
                AgentChatRequest(message="hi", session_id=bad)

    def test_session_id_rejects_too_long(self):
        with self.assertRaises(ValidationError):
            AgentChatRequest(message="hi", session_id="a" * 65)

    # ---- confirm_action_id ----

    def test_confirm_action_id_too_long(self):
        with self.assertRaises(ValidationError):
            AgentChatRequest(
                message="",
                confirm_action_id="a" * 33,
                decision="accept",
            )

    # ---- decision 与 confirm_action_id 协同 ----

    def test_decision_alone_is_rejected(self):
        with self.assertRaises(ValidationError):
            AgentChatRequest(message="hi", decision="accept")

    def test_confirm_without_decision_is_rejected(self):
        with self.assertRaises(ValidationError):
            AgentChatRequest(message="hi", confirm_action_id="abc123")

    def test_decision_with_unknown_value_is_rejected(self):
        with self.assertRaises(ValidationError):
            AgentChatRequest(
                message="",
                confirm_action_id="abc",
                decision="maybe",
            )

    def test_full_confirm_payload_is_accepted(self):
        req = AgentChatRequest(
            message="",
            confirm_action_id="abc123",
            decision="reject",
        )
        self.assertEqual(req.decision, "reject")
        self.assertEqual(req.confirm_action_id, "abc123")


class AgentChatAPIValidationTests(TestCase):
    """FastAPI 层校验。保证 schema 限制真在路由上生效（422 而不是 200/500）。"""

    def setUp(self):
        self.application = FastAPI()
        self.application.include_router(agent.router)
        # 不调用真实 LLM：把 agent.handle 替换成已知返回
        self.handle_patch = patch(
            "app.api.agent.agent.handle",
            return_value=None,  # 不该走到这里
        )
        # 真实 endpoint 会调 agent.handle，固定 patch 一个正常返回
        from app.agent.loop import AgentResult

        self.fake_result = AgentResult(reply="ok", session_id="sid")
        self.handle_patch = patch(
            "app.api.agent.agent.handle",
            return_value=self.fake_result,
        )
        # 数据库相关辅助也得桩一下
        self.run_patch = patch(
            "app.api.agent.agent_run_service.record_run",
            return_value=type("_Run", (), {"id": "run-1"})(),
        )
        # TestClient 创建时 FastAPI 会做 lifespan，但我们的最小 app 不接数据库；
        # 简单替换 get_db 以提供工作 session
        from app.database import get_db
        from tests.helpers import make_session

        self.db = make_session()

        def _override_get_db():
            try:
                yield self.db
            finally:
                pass  # 留给 TestCase 清理

        self.application.dependency_overrides[get_db] = _override_get_db
        self.client = TestClient(self.application)
        self.handle_patch.start()
        self.run_patch.start()

    def tearDown(self):
        self.handle_patch.stop()
        self.run_patch.stop()

    def test_post_chat_with_huge_message_returns_422(self):
        response = self.client.post(
            "/api/agent/chat",
            json={"message": "x" * 20001, "session_id": "sid"},
        )
        self.assertEqual(response.status_code, 422)
        body = response.json()
        # Pydantic 错误里能看到具体限制
        joined = str(body)
        self.assertIn("20000", joined)

    def test_post_chat_with_empty_message_returns_422(self):
        response = self.client.post(
            "/api/agent/chat",
            json={"message": "", "session_id": "sid"},
        )
        self.assertEqual(response.status_code, 422)

    def test_post_chat_with_bad_session_id_returns_422(self):
        response = self.client.post(
            "/api/agent/chat",
            json={"message": "hi", "session_id": "session/with/slash"},
        )
        self.assertEqual(response.status_code, 422)

    def test_post_chat_with_decision_without_confirm_returns_422(self):
        response = self.client.post(
            "/api/agent/chat",
            json={"message": "hi", "decision": "accept"},
        )
        self.assertEqual(response.status_code, 422)

    def test_post_chat_at_length_boundary_succeeds(self):
        """恰好 20000 字符应放行（不让一刀切导致用户体验割裂）。"""
        response = self.client.post(
            "/api/agent/chat",
            json={"message": "x" * 20000, "session_id": "sid"},
        )
        self.assertEqual(response.status_code, 200)
