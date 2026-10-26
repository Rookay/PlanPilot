# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：test_summary_layer.py
"""层二摘要：折叠时机、增量覆盖、降级路径与 prompt 注入。

这一层最容易写错的地方不是"摘要写得好不好"，而是**什么时候该写**。三个约束
必须同时成立：没到阈值不能白调模型；折叠范围只能前进不能回退；折叠失败也不能
把整轮对话带崩。这里逐条锁住。
"""

import json
from unittest import TestCase

from app.agent.confirmation import PendingActionStore
from app.agent.loop import AgentLoop
from app.agent.memory import Memory
from app.agent.summary import (
    SECTION_TITLES,
    fold_history,
    render_summary,
    summarize,
)
from app.llm.base import LLMProvider, LLMResponse
from app.llm.mock import MockProvider
from app.services import chat_service, summary_service
from app.tools.defaults import build_default_registry
from tests.helpers import make_session


class _StructuredProvider(LLMProvider):
    """只实现 structured 的假 Provider，记录调用次数和最后一次输入。"""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0
        self.last_messages = None

    def chat(self, messages, tools=None):
        return LLMResponse(text="ok")

    def structured(self, messages, output_schema):
        self.calls += 1
        self.last_messages = messages
        if self.error is not None:
            raise self.error
        return self.payload


def _seed(db, session_id, count, start=0):
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        chat_service.append_message(db, session_id, role, f"第{start + index}条消息")
    db.commit()


def _five_sections(**overrides):
    payload = {key: [] for key, _ in SECTION_TITLES}
    payload["constraints"] = ["周五之前必须交初稿"]
    payload.update(overrides)
    return payload


class FoldTests(TestCase):
    def setUp(self):
        self.db = make_session()

    def _fold(self, provider, session_id="s1", window=60, keep=10, minimum=8):
        return fold_history(
            self.db,
            provider,
            session_id,
            window_limit=window,
            keep_messages=keep,
            min_fold_messages=minimum,
        )

    def test_below_threshold_does_not_fold(self):
        _seed(self.db, "s1", 15)
        provider = _StructuredProvider(payload=_five_sections())

        result = self._fold(provider)

        self.assertEqual(result.folded, 0)
        self.assertEqual(len(result.history), 15)
        self.assertIsNone(result.summary_text)
        # 没到阈值就不该花一次模型调用——这是摘要层最容易漏的成本。
        self.assertEqual(provider.calls, 0)
        self.assertIsNone(summary_service.get_summary(self.db, "s1"))

    def test_fold_writes_summary_and_keeps_tail(self):
        _seed(self.db, "s1", 30)
        provider = _StructuredProvider(payload=_five_sections())

        result = self._fold(provider)

        self.assertEqual(result.folded, 20)
        self.assertEqual(len(result.history), 10)
        self.assertEqual(result.history[0]["content"], "第20条消息")

        record = summary_service.get_summary(self.db, "s1")
        self.assertIsNotNone(record)
        self.assertEqual(record.covered_count, 20)
        self.assertEqual(record.source, "model")
        self.assertIn("周五之前必须交初稿", result.summary_text)

    def test_second_fold_only_covers_new_messages(self):
        _seed(self.db, "s1", 30)
        provider = _StructuredProvider(payload=_five_sections())
        self._fold(provider)

        _seed(self.db, "s1", 10, start=30)
        result = self._fold(provider)

        # 已折过的 20 条不再重复折：这次只吃第 21~40 条里的溢出部分。
        self.assertEqual(result.folded, 10)
        record = summary_service.get_summary(self.db, "s1")
        self.assertEqual(record.covered_count, 30)

    def test_model_failure_falls_back_to_deterministic(self):
        _seed(self.db, "s1", 30)
        provider = _StructuredProvider(error=RuntimeError("模型挂了"))

        result = self._fold(provider)

        record = summary_service.get_summary(self.db, "s1")
        self.assertIsNotNone(record)
        self.assertEqual(record.source, "deterministic")
        self.assertIn("digest", json.loads(record.content))
        # 降级也要前进：否则每次请求都会重试同一段折叠，白烧调用还看不清原因。
        self.assertEqual(result.folded, 20)

    def test_empty_model_output_falls_back(self):
        _seed(self.db, "s1", 30)
        provider = _StructuredProvider(payload={})

        self._fold(provider)

        record = summary_service.get_summary(self.db, "s1")
        self.assertEqual(record.source, "deterministic")

    def test_model_output_is_trimmed(self):
        _seed(self.db, "s1", 30)
        payload = _five_sections(
            constraints=["约" * 300] + [f"约束{index}" for index in range(10)]
        )
        provider = _StructuredProvider(payload=payload)

        self._fold(provider)

        record = summary_service.get_summary(self.db, "s1")
        data = json.loads(record.content)
        self.assertEqual(len(data["constraints"]), 6)
        self.assertLessEqual(len(data["constraints"][0]), 120)

    def test_prompt_carries_previous_summary(self):
        _seed(self.db, "s1", 30)
        provider = _StructuredProvider(payload=_five_sections())
        self._fold(provider)

        _seed(self.db, "s1", 10, start=30)
        self._fold(provider)

        prompt = provider.last_messages[1]["content"]
        self.assertIn("【已有摘要】", prompt)
        self.assertIn("周五之前必须交初稿", prompt)

    def test_summary_budget_bounds_transcript(self):
        # 30 条长消息，全量塞进摘要输入会顶不住预算，只应装最新的一段。
        for index in range(30):
            chat_service.append_message(
                self.db, "s1", "user", f"第{index}条：" + "很长的约束描述" * 80
            )
        self.db.commit()
        provider = _StructuredProvider(payload=_five_sections())

        result = self._fold(provider)

        prompt = provider.last_messages[1]["content"]
        self.assertIn("[20] user", prompt)  # 最新的一定在
        self.assertNotIn("[1] user", prompt)  # 最早的被预算挡在外面

        # 关键：被挡掉的那几条不能被宣告成"已覆盖"，否则它们既进不了摘要也回不到
        # 窗口，等于永久蒸发。这里断言折叠条数 + 剩余历史 == 全部消息。
        self.assertEqual(result.folded + len(result.history), 30)
        self.assertLess(result.folded, 20)


class SummaryStorageTests(TestCase):
    def setUp(self):
        self.db = make_session()

    def test_covered_position_never_rewinds(self):
        summary_service.save_summary(
            self.db,
            "s1",
            content=json.dumps({"digest": "第一版"}, ensure_ascii=False),
            covered_to_message_id=40,
            covered_count=40,
            source="deterministic",
        )
        self.db.commit()

        # 并发请求各自算出一次折叠，后写的范围更小，不能把摘要改回去。
        summary_service.save_summary(
            self.db,
            "s1",
            content=json.dumps({"digest": "旧版"}, ensure_ascii=False),
            covered_to_message_id=20,
            covered_count=20,
            source="deterministic",
        )
        self.db.commit()

        record = summary_service.get_summary(self.db, "s1")
        self.assertEqual(record.covered_to_message_id, 40)
        self.assertIn("第一版", record.content)

    def test_clear_summary_removes_row(self):
        summary_service.save_summary(
            self.db,
            "s1",
            content=json.dumps({"digest": "x"}),
            covered_to_message_id=10,
            covered_count=10,
            source="deterministic",
        )
        self.db.commit()

        self.assertEqual(summary_service.clear_summary(self.db, "s1"), 1)
        self.db.commit()
        self.assertIsNone(summary_service.get_summary(self.db, "s1"))


class RenderTests(TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(render_summary(None))

    def test_broken_json_is_ignored(self):
        record = type("R", (), {"content": "{不是 JSON", "covered_count": 3})()
        self.assertIsNone(render_summary(record))

    def test_digest_renders_as_auto_compressed(self):
        record = type(
            "R",
            (),
            {"content": json.dumps({"digest": "用户要周五交稿"}), "covered_count": 12},
        )()
        text = render_summary(record)
        self.assertIn("自动压缩", text)
        self.assertIn("用户要周五交稿", text)

    def test_sections_render_with_titles(self):
        record = type(
            "R",
            (),
            {
                "content": json.dumps(
                    {"constraints": ["周五交稿"], "decisions": [], "entities": ["论文"]}
                ),
                "covered_count": 8,
            },
        )()
        text = render_summary(record)
        self.assertIn("约束：周五交稿", text)
        self.assertIn("关键实体：论文", text)
        self.assertNotIn("已定事项", text)  # 空段不渲染，省 token

    def test_all_empty_returns_none(self):
        record = type(
            "R", (), {"content": json.dumps({key: [] for key, _ in SECTION_TITLES}), "covered_count": 4}
        )()
        self.assertIsNone(render_summary(record))


class SummarizeDirectTests(TestCase):
    def test_empty_messages_return_empty(self):
        provider = _StructuredProvider(payload=_five_sections())
        content, source = summarize(provider, None, [])
        self.assertEqual((content, source), ("", "empty"))
        self.assertEqual(provider.calls, 0)


class SummaryInjectionTests(TestCase):
    def setUp(self):
        self.db = make_session()
        self.loop = AgentLoop(
            llm=MockProvider(),
            registry=build_default_registry(),
            memory=Memory(),
            confirmation_store=PendingActionStore(),
        )

    def test_summary_is_injected_before_window(self):
        _seed(self.db, "s9", 30)

        messages = self.loop._build_messages(self.db, "s9", "继续")

        system = messages[0]["content"]
        # MockProvider.structured 返回空 dict，走确定性降级——离线时这条路径必须能用。
        self.assertIn("更早对话", system)
        self.assertIn("自动压缩", system)

        # 摘要之后只发保留窗口内的消息，当前这句还没落库，单独追加在末尾。
        self.assertEqual(len(messages) - 2, self.loop.HISTORY_MESSAGE_LIMIT)
        self.assertEqual(messages[-1]["content"], "继续")

    def test_no_summary_when_history_is_short(self):
        _seed(self.db, "s10", 6)

        messages = self.loop._build_messages(self.db, "s10", "继续")

        self.assertNotIn("更早对话", messages[0]["content"])
        self.assertEqual(len(messages), 8)  # system + 6 条历史 + 当前
