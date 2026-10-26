# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：test_memory_facts.py
"""层三事实区：候选制、去重、转正、否决与注入。

事实区最大的风险不是"记不住"，而是"记错了还每次都当真"。所以这里的测试重点
全在**准入**：什么条件下允许一条推断进入 system prompt，什么条件下必须退回候选，
用户否决之后还能不能被自动复活。
"""

import json
from unittest import TestCase

from app.agent.confirmation import PendingActionStore
from app.agent.loop import AgentLoop
from app.agent.memory import Memory
from app.agent.summary import SECTION_TITLES, fold_history
from app.llm.base import LLMProvider, LLMResponse
from app.llm.mock import MockProvider
from app.models.fact import STATUS_CANDIDATE, STATUS_CONFIRMED, STATUS_REJECTED
from app.services import chat_service, fact_service
from app.tools.defaults import build_default_registry
from tests.helpers import make_session


class _StructuredProvider(LLMProvider):
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0

    def chat(self, messages, tools=None):
        return LLMResponse(text="ok")

    def structured(self, messages, output_schema):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


def _payload(facts=None, **overrides):
    data = {key: [] for key, _ in SECTION_TITLES}
    data["constraints"] = ["周五之前必须交初稿"]
    data["facts"] = facts or []
    data.update(overrides)
    return data


def _seed(db, session_id, count, start=0):
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        chat_service.append_message(db, session_id, role, f"第{start + index}条消息")
    db.commit()


class FactStoreTests(TestCase):
    def setUp(self):
        self.db = make_session()

    def test_first_mention_stays_candidate(self):
        fact_service.record_facts(
            self.db, [{"fact": "导师姓王", "category": "entity"}], session_id="s1"
        )
        self.db.commit()

        records = fact_service.list_facts(self.db)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, STATUS_CANDIDATE)
        self.assertEqual(records[0].hit_count, 1)
        # 候选不注入——这是候选制存在的全部意义。
        self.assertIsNone(fact_service.render_facts(fact_service.confirmed_facts(self.db)))

    def test_second_mention_confirms(self):
        fact_service.record_facts(
            self.db, [{"fact": "导师姓王", "category": "entity"}], session_id="s1"
        )
        fact_service.record_facts(
            self.db, [{"fact": "导师姓王", "category": "entity"}], session_id="s1"
        )
        self.db.commit()

        records = fact_service.list_facts(self.db)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, STATUS_CONFIRMED)
        self.assertEqual(records[0].hit_count, 2)

    def test_punctuation_variants_are_same_fact(self):
        fact_service.record_facts(self.db, [{"fact": "每周三晚上有组会"}])
        fact_service.record_facts(self.db, [{"fact": "每周三晚上，有组会。"}])
        self.db.commit()

        records = fact_service.list_facts(self.db)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, STATUS_CONFIRMED)

    def test_rejected_fact_never_revives(self):
        fact_service.record_facts(self.db, [{"fact": "用户喜欢早上跑步"}])
        self.db.commit()
        record = fact_service.list_facts(self.db)[0]
        fact_service.set_status(self.db, record.id, STATUS_REJECTED)
        self.db.commit()

        # 再提三次也不该回到候选，否则"否决"就只是一次性的。
        for _ in range(3):
            fact_service.record_facts(self.db, [{"fact": "用户喜欢早上跑步"}])
        self.db.commit()

        record = fact_service.list_facts(self.db)[0]
        self.assertEqual(record.status, STATUS_REJECTED)
        self.assertEqual(record.hit_count, 1)

    def test_manual_confirm_lifts_hit_count(self):
        fact_service.record_facts(self.db, [{"fact": "住在杭州"}])
        self.db.commit()
        record = fact_service.list_facts(self.db)[0]

        fact_service.set_status(self.db, record.id, STATUS_CONFIRMED)
        self.db.commit()

        record = fact_service.list_facts(self.db)[0]
        self.assertEqual(record.status, STATUS_CONFIRMED)
        self.assertGreaterEqual(record.hit_count, 2)

    def test_delete_removes_row(self):
        fact_service.record_facts(self.db, [{"fact": "住在杭州"}])
        self.db.commit()
        record = fact_service.list_facts(self.db)[0]

        self.assertTrue(fact_service.delete_fact(self.db, record.id))
        self.db.commit()
        self.assertEqual(fact_service.list_facts(self.db), [])

    def test_unknown_status_is_rejected(self):
        fact_service.record_facts(self.db, [{"fact": "住在杭州"}])
        self.db.commit()
        record = fact_service.list_facts(self.db)[0]

        self.assertIsNone(fact_service.set_status(self.db, record.id, "whatever"))

    def test_overlong_fact_is_dropped(self):
        fact_service.record_facts(self.db, [{"fact": "很长的句子" * 60}])
        self.db.commit()
        self.assertEqual(fact_service.list_facts(self.db), [])


class FactExtractionTests(TestCase):
    def setUp(self):
        self.db = make_session()

    def _fold(self, provider, session_id="s1"):
        return fold_history(
            self.db,
            provider,
            session_id,
            window_limit=60,
            keep_messages=10,
            min_fold_messages=8,
        )

    def test_fold_records_extracted_facts(self):
        _seed(self.db, "s1", 30)
        provider = _StructuredProvider(
            payload=_payload(facts=[{"fact": "导师姓王", "category": "entity"}])
        )

        self._fold(provider)

        records = fact_service.list_facts(self.db)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].fact, "导师姓王")
        self.assertEqual(records[0].status, STATUS_CANDIDATE)

    def test_deterministic_fallback_writes_no_facts(self):
        _seed(self.db, "s1", 30)
        provider = _StructuredProvider(error=RuntimeError("模型挂了"))

        self._fold(provider)

        # 离线模式没有归纳能力，就不该往事实区写东西——宁缺勿造。
        self.assertEqual(fact_service.list_facts(self.db), [])

    def test_facts_are_capped_and_deduplicated(self):
        _seed(self.db, "s1", 30)
        provider = _StructuredProvider(
            payload=_payload(
                facts=[
                    {"fact": f"事实{index}", "category": "entity"} for index in range(6)
                ]
            )
        )

        self._fold(provider)

        self.assertEqual(len(fact_service.list_facts(self.db)), 3)


class FactInjectionTests(TestCase):
    def setUp(self):
        self.db = make_session()
        self.loop = AgentLoop(
            llm=MockProvider(),
            registry=build_default_registry(),
            memory=Memory(),
            confirmation_store=PendingActionStore(),
        )

    def test_confirmed_fact_enters_system_prompt(self):
        fact_service.record_facts(self.db, [{"fact": "导师姓王"}])
        fact_service.record_facts(self.db, [{"fact": "导师姓王"}])
        self.db.commit()

        messages = self.loop._build_messages(self.db, "s1", "帮我约导师")

        self.assertIn("长期事实", messages[0]["content"])
        self.assertIn("导师姓王", messages[0]["content"])

    def test_candidate_stays_out_of_prompt(self):
        fact_service.record_facts(self.db, [{"fact": "导师姓王"}])
        self.db.commit()

        messages = self.loop._build_messages(self.db, "s1", "帮我约导师")

        self.assertNotIn("导师姓王", messages[0]["content"])

    def test_rejected_fact_disappears_from_prompt(self):
        fact_service.record_facts(self.db, [{"fact": "导师姓王"}])
        fact_service.record_facts(self.db, [{"fact": "导师姓王"}])
        self.db.commit()
        record = fact_service.list_facts(self.db)[0]

        fact_service.set_status(self.db, record.id, STATUS_REJECTED)
        self.db.commit()

        messages = self.loop._build_messages(self.db, "s1", "帮我约导师")
        self.assertNotIn("导师姓王", messages[0]["content"])

    def test_render_limits_line_count(self):
        facts = [
            type("F", (), {"fact": f"事实{index}"})() for index in range(3)
        ]
        text = fact_service.render_facts(facts)
        self.assertEqual(text.count("\n- "), 3)
        self.assertIn("以工具查询结果为准", text)


class FactRouteTests(TestCase):
    def test_route_is_registered(self):
        from app.main import app

        # 用 OpenAPI 而不是遍历 app.routes：后者里有 _IncludedRouter 这类中间对象，
        # 展开后的路径只有在 schema 里才是准的。
        paths = set(app.openapi()["paths"])
        self.assertIn("/api/memory/facts", paths)
        self.assertIn("/api/memory/facts/{fact_id}", paths)
