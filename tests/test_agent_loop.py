import io
import socket
import time
import urllib.error
from unittest import TestCase
from unittest.mock import patch

from app.agent.confirmation import PendingActionStore
from app.agent.loop import (
    AgentLoop,
    LLMStatus,
    STATE_AUTH_FAILED,
    STATE_OK,
    STATE_RATE_LIMITED,
    STATE_TRANSIENT,
)
from app.agent.memory import Memory
from app.llm.base import LLMProvider
from app.llm.exceptions import (
    LLMAuthError,
    LLMRateLimitError,
    LLMTransientError,
)
from app.llm.openai_compat import OpenAICompatibleProvider
from app.skills.planner_skills import build_default_skills
from app.tools.defaults import build_default_registry


class _FailingProvider(LLMProvider):
    """按指定的异常类型抛出,用于驱动 AgentLoop 的错误分支。"""

    def __init__(self, exc: Exception):
        self._exc = exc

    def chat(self, messages, tools=None):
        raise self._exc

    def structured(self, messages, output_schema):
        raise self._exc


def _build_loop(llm: LLMProvider | None = None) -> AgentLoop:
    return AgentLoop(
        llm=llm or _FailingProvider(RuntimeError("unused")),
        registry=build_default_registry(),
        memory=Memory(),
        confirmation_store=PendingActionStore(),
        skill_registry=build_default_skills(),
    )


class AgentLoopStatusTests(TestCase):
    """AgentLoop 的状态机与退避算法。"""

    def setUp(self):
        self.loop = _build_loop()

    def test_initial_state_is_ok(self):
        self.assertEqual(self.loop.llm_status.state, STATE_OK)
        self.assertEqual(self.loop.llm_status.consecutive_failures, 0)
        self.assertEqual(self.loop.llm_status.fallback_until, 0.0)

    def test_auth_error_marks_provider_disabled(self):
        exc = LLMAuthError("invalid api key")
        self.loop._mark_auth_failed(exc)
        status = self.loop.llm_status
        self.assertEqual(status.state, STATE_AUTH_FAILED)
        self.assertEqual(status.last_error_type, "auth")
        self.assertEqual(status.last_error_message, "invalid api key")
        # 鉴权失败:不设 fallback_until,永不自动恢复。
        self.assertEqual(status.fallback_until, float("inf"))

    def test_rate_limit_uses_exponential_backoff(self):
        # 关掉抖动,让等待时长可断言。
        with patch("app.agent.loop.random.uniform", return_value=0):
            base = self.loop.BACKOFF_BASE_SECONDS
            for idx in range(1, 4):
                self.loop._mark_failure("rate_limit", LLMRateLimitError("429"))
                expected = min(base * (2 ** (idx - 1)), self.loop.BACKOFF_MAX_SECONDS)
                actual = self.loop.llm_status.fallback_until - time.monotonic()
                self.assertAlmostEqual(actual, expected, places=1)

    def test_transient_error_uses_same_backoff(self):
        with patch("app.agent.loop.random.uniform", return_value=0):
            self.loop._mark_failure("transient", LLMTransientError("502"))
            status = self.loop.llm_status
            self.assertEqual(status.state, STATE_TRANSIENT)
            self.assertEqual(status.last_error_type, "transient")
            expected = self.loop.BACKOFF_BASE_SECONDS
            actual = status.fallback_until - time.monotonic()
            self.assertAlmostEqual(actual, expected, places=1)

    def test_backoff_caps_at_max(self):
        with patch("app.agent.loop.random.uniform", return_value=0):
            for _ in range(10):
                self.loop._mark_failure("transient", LLMTransientError("boom"))
            cap = self.loop.BACKOFF_MAX_SECONDS
            actual = self.loop.llm_status.fallback_until - time.monotonic()
            self.assertLessEqual(actual, cap + 0.5)

    def test_consecutive_failures_increment(self):
        self.loop._mark_failure("transient", LLMTransientError("e"))
        self.loop._mark_failure("rate_limit", LLMRateLimitError("e"))
        self.loop._mark_failure("transient", LLMTransientError("e"))
        self.assertEqual(self.loop.llm_status.consecutive_failures, 3)

    def test_success_resets_status(self):
        # 先积累几次失败
        self.loop._mark_failure("rate_limit", LLMRateLimitError("e"))
        self.loop._mark_failure("transient", LLMTransientError("e"))
        # 成功一次
        self.loop._record_success()
        status = self.loop.llm_status
        self.assertEqual(status.state, STATE_OK)
        self.assertEqual(status.consecutive_failures, 0)
        self.assertIsNone(status.last_error_type)
        self.assertIsNone(status.last_error_message)
        self.assertEqual(status.fallback_until, 0.0)
        self.assertGreater(status.last_success_at, 0)

    def test_pick_provider_returns_real_when_state_ok(self):
        real = _FailingProvider(RuntimeError("not used"))
        loop = _build_loop(real)
        self.assertIs(loop._pick_provider(), real)

    def test_pick_provider_returns_fallback_during_cooldown(self):
        loop = _build_loop(_FailingProvider(RuntimeError("not used")))
        loop._mark_failure("transient", LLMTransientError("e"))
        self.assertIs(loop._pick_provider(), loop.fallback_provider)

    def test_pick_provider_resumes_real_when_cooldown_past(self):
        loop = _build_loop(_FailingProvider(RuntimeError("not used")))
        loop.llm_status.fallback_until = time.monotonic() - 1  # 已过期
        self.assertIs(loop._pick_provider(), loop.llm)


class OpenAICompatClassificationTests(TestCase):
    """OpenAICompatibleProvider._post 把 HTTP / 网络异常分类成对应的 LLMError。"""

    def _provider(self) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(
            base_url="http://example.invalid/v1",
            api_key="test",
            model="test-model",
            timeout=5,
        )

    def _http_error(self, code: int) -> urllib.error.HTTPError:
        body = io.BytesIO(b'{"error":"detail"}')
        return urllib.error.HTTPError(
            url="http://example.invalid/v1/chat/completions",
            code=code,
            msg="status",
            hdrs={},
            fp=body,
        )

    def test_401_raises_auth_error(self):
        with patch(
            "app.llm.openai_compat.urllib.request.urlopen",
            side_effect=self._http_error(401),
        ):
            with self.assertRaises(LLMAuthError):
                self._provider().chat([{"role": "user", "content": "hi"}])

    def test_403_raises_auth_error(self):
        with patch(
            "app.llm.openai_compat.urllib.request.urlopen",
            side_effect=self._http_error(403),
        ):
            with self.assertRaises(LLMAuthError):
                self._provider().chat([{"role": "user", "content": "hi"}])

    def test_429_raises_rate_limit_error(self):
        with patch(
            "app.llm.openai_compat.urllib.request.urlopen",
            side_effect=self._http_error(429),
        ):
            with self.assertRaises(LLMRateLimitError):
                self._provider().chat([{"role": "user", "content": "hi"}])

    def test_500_raises_transient_error(self):
        with patch(
            "app.llm.openai_compat.urllib.request.urlopen",
            side_effect=self._http_error(500),
        ):
            with self.assertRaises(LLMTransientError):
                self._provider().chat([{"role": "user", "content": "hi"}])

    def test_url_error_raises_transient(self):
        with patch(
            "app.llm.openai_compat.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with self.assertRaises(LLMTransientError):
                self._provider().chat([{"role": "user", "content": "hi"}])

    def test_socket_timeout_raises_transient(self):
        with patch(
            "app.llm.openai_compat.urllib.request.urlopen",
            side_effect=urllib.error.URLError(socket.timeout("read timed out")),
        ):
            with self.assertRaises(LLMTransientError):
                self._provider().chat([{"role": "user", "content": "hi"}])


class AgentLoopEndToEndFallbackTests(TestCase):
    """AgentLoop.run 在真实 Provider 抛错时正确切到 MockProvider,且更新状态。"""

    def setUp(self):
        from tests.helpers import make_session

        self.db = make_session()

    def tearDown(self):
        self.db.close()

    def test_auth_error_falls_back_and_disables_real(self):
        real = _FailingProvider(LLMAuthError("bad key"))
        loop = _build_loop(real)

        result = loop.run(self.db, "看看任务", session_id="s1")

        # 仍然走完一次对话(用 MockProvider 兜底),但状态被永久置为 auth_failed。
        status = loop.llm_status
        self.assertEqual(status.state, STATE_AUTH_FAILED)
        self.assertEqual(status.last_error_type, "auth")
        self.assertEqual(status.fallback_until, float("inf"))
        # 用户能看到一次正常回复,没有崩溃。
        self.assertTrue(result.reply)

    def test_transient_error_uses_backoff(self):
        real = _FailingProvider(LLMTransientError("502"))
        loop = _build_loop(real)

        with patch("app.agent.loop.random.uniform", return_value=0):
            loop.run(self.db, "安排一下", session_id="s2")

        status = loop.llm_status
        self.assertEqual(status.state, STATE_TRANSIENT)
        self.assertEqual(status.consecutive_failures, 1)
        self.assertGreater(status.fallback_until, time.monotonic())


class StatusEndpointTests(TestCase):
    """/api/agent/status 返回结构化字段。"""

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import warnings

        warnings.filterwarnings(
            "ignore",
            message="Using `httpx` with `starlette.testclient` is deprecated",
        )
        from app.api import agent as agent_router
        from app.database import get_db
        from tests.helpers import make_session

        self.db = make_session()
        application = FastAPI()
        application.include_router(agent_router.router)

        def _override():
            yield self.db

        application.dependency_overrides[get_db] = _override
        self.client = TestClient(application)

        # 每个用例前重置全局 Agent 状态,避免被其它测试污染。
        from app.agent.factory import agent as global_agent

        global_agent.loop.llm_status = LLMStatus()

    def tearDown(self):
        self.client.close()
        self.db.close()

    def test_status_exposes_structured_fields_when_ok(self):
        response = self.client.get("/api/agent/status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for key in (
            "state",
            "last_error_type",
            "last_error",
            "fallback_active",
            "next_retry_at",
            "consecutive_failures",
            "max_iterations",
            "max_tool_calls",
        ):
            self.assertIn(key, body)
        self.assertEqual(body["state"], STATE_OK)
        self.assertFalse(body["fallback_active"])
        self.assertIsNone(body["last_error_type"])
        self.assertIsNone(body["next_retry_at"])

    def test_status_reflects_auth_failure(self):
        from app.agent.factory import agent as global_agent

        global_agent.loop._mark_auth_failed(LLMAuthError("bad"))
        response = self.client.get("/api/agent/status")
        body = response.json()
        self.assertEqual(body["state"], STATE_AUTH_FAILED)
        self.assertEqual(body["last_error_type"], "auth")
        self.assertTrue(body["fallback_active"])
        # 鉴权失败 next_retry_at 应该是 null(永不自动恢复)。
        self.assertIsNone(body["next_retry_at"])

    def test_status_reflects_rate_limit_with_retry_window(self):
        from app.agent.factory import agent as global_agent

        with patch("app.agent.loop.random.uniform", return_value=0):
            global_agent.loop._mark_failure(
                "rate_limit", LLMRateLimitError("429")
            )
        response = self.client.get("/api/agent/status")
        body = response.json()
        self.assertEqual(body["state"], STATE_RATE_LIMITED)
        self.assertEqual(body["consecutive_failures"], 1)
        # fallback_until 是 monotonic 时间戳,前端应看到 next_retry_at。
        self.assertIsNotNone(body["next_retry_at"])