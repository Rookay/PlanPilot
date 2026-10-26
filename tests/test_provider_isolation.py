"""本次请求用了哪个模型，必须跟着返回值走。

``AgentLoop`` 是全局单例（``app/agent/factory.py`` 里只建一次），原先
``self.last_run_provider`` 挂在实例上：并发时后一个请求覆盖前一个，界面就会把
A 请求的结果标成 B 请求用的模型。``llm_status``（熔断器）共享是刻意的，
"这次用的哪个 provider" 不是——它是请求级数据。

这组测试用同一份顺序证据：慢请求先开始、快请求随后失败降级，
两条结果各自报自己的 provider。
"""

import threading
import time
from unittest import TestCase

from app.agent.confirmation import PendingActionStore
from app.agent.loop import AgentLoop
from app.agent.memory import Memory
from app.llm.base import LLMProvider, LLMResponse
from app.llm.exceptions import LLMTransientError
from app.llm.mock import MockProvider
from app.skills.planner_skills import build_default_skills
from app.tools.defaults import build_default_registry
from tests.helpers import make_session


class _EchoProvider(LLMProvider):
    """永远立刻返回纯文本，用来确认正常路径的 provider 名。"""

    def chat(self, messages, tools=None):
        return LLMResponse(text="正常回复", usage={"total_tokens": 3})

    def structured(self, messages, output_schema):
        return {}


class _AlwaysFailProvider(LLMProvider):
    """每次都失败，用来验证降级路径报的是降级 provider。"""

    def chat(self, messages, tools=None):
        raise LLMTransientError("一直失败")

    def structured(self, messages, output_schema):
        raise LLMTransientError("一直失败")


class _SlowThenFailProvider(LLMProvider):
    """第一次调用慢速成功，之后的调用立刻失败。

    并发测试靠这个造出"一次正常 + 一次降级"，且顺序可控：慢调用会先占住
    provider，失败的那次等它开始之后才发生。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._calls = 0
        self.first_started = threading.Event()

    def chat(self, messages, tools=None):
        with self._lock:
            self._calls += 1
            index = self._calls
        if index == 1:
            self.first_started.set()
            time.sleep(0.25)
            return LLMResponse(text="慢但正常", usage={"total_tokens": 3})
        # 等第一次真的进了 provider，保证"慢请求先开始"这个前提成立。
        self.first_started.wait(timeout=10)
        raise LLMTransientError("模拟第二次调用失败")

    def structured(self, messages, output_schema):
        raise LLMTransientError("模拟失败")


def _build_loop(llm: LLMProvider) -> AgentLoop:
    return AgentLoop(
        llm=llm,
        registry=build_default_registry(),
        memory=Memory(),
        confirmation_store=PendingActionStore(),
        skill_registry=build_default_skills(),
    )


class ProviderNameOnResultTests(TestCase):
    def setUp(self):
        self.db = make_session()

    def tearDown(self):
        self.db.close()

    def test_result_reports_the_provider_that_served_it(self):
        loop = _build_loop(_EchoProvider())
        result = loop.run(self.db, "你好", "provider-1")

        self.assertEqual(result.reply, "正常回复")
        self.assertEqual(result.provider_name, "_EchoProvider")

    def test_fallback_result_reports_the_fallback_provider(self):
        loop = _build_loop(_AlwaysFailProvider())
        result = loop.run(self.db, "你好", "provider-2")

        self.assertEqual(result.provider_name, MockProvider.__name__)

    def test_loop_holds_no_request_scoped_provider_attribute(self):
        loop = _build_loop(_EchoProvider())
        self.assertFalse(
            hasattr(loop, "last_run_provider"),
            "provider 名不能存在单例上，否则并发请求会互相覆盖",
        )


class ConcurrentProviderIsolationTests(TestCase):
    """两个请求同时进行时，各自的结果只报自己的 provider。"""

    def setUp(self):
        self.db = make_session()

    def tearDown(self):
        self.db.close()

    def test_slow_and_failing_requests_report_their_own_provider(self):
        llm = _SlowThenFailProvider()
        loop = _build_loop(llm)
        results = {}
        errors = []

        def slow_request():
            db = make_session()
            try:
                # 先跑起来，占住 provider。
                results["slow"] = loop.run(db, "第一条", "provider-slow")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                db.close()

        def failing_request():
            db = make_session()
            try:
                # 等慢请求真的进入 provider，再制造降级。
                llm.first_started.wait(timeout=10)
                results["failing"] = loop.run(db, "第二条", "provider-fail")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                db.close()

        slow = threading.Thread(target=slow_request, daemon=True)
        fast = threading.Thread(target=failing_request, daemon=True)
        slow.start()
        fast.start()
        slow.join(timeout=60)
        fast.join(timeout=60)

        self.assertFalse(errors, f"并发请求出错：{errors}")
        self.assertEqual(set(results), {"slow", "failing"})
        # 慢请求在失败发生之前就拿到了 provider，它的结果不能被后来的降级改写。
        self.assertEqual(results["slow"].provider_name, "_SlowThenFailProvider")
        self.assertEqual(results["failing"].provider_name, MockProvider.__name__)
