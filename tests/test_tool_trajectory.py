"""工具调用轨迹：配对、落库、回看。

这层补的是层一（全量流水）里缺的一截——``chat_messages`` 只存文本，中间那几轮
工具调用原先只活在单次请求的局部 ``messages`` 里，请求一结束就没了。
"""

import json
from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

from app.agent.confirmation import PendingActionStore
from app.agent.loop import AgentLoop
from app.agent.memory import Memory
from app.llm.mock import MockProvider
from app.services import chat_service
from app.skills.planner_skills import build_default_skills
from app.tools.defaults import build_default_registry
from tests.helpers import make_session


def _call(call_id, name, arguments="{}"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _observation(call_id, name, payload):
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload,
    }


class TrajectoryExtractionTests(TestCase):
    """只测配对逻辑，不碰数据库。"""

    def test_call_and_observation_are_paired(self):
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [_call("c1", "get_tasks")]},
            _observation(
                "c1", "get_tasks", {"success": True, "summary": "查到 3 个任务"}
            ),
        ]

        records = AgentLoop._extract_trajectory(messages)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tool_call_id"], "c1")
        self.assertEqual(records[0]["name"], "get_tasks")
        self.assertTrue(records[0]["success"])
        self.assertEqual(records[0]["summary"], "查到 3 个任务")

    def test_interrupted_call_is_kept_with_reason(self):
        # 模型调了但没拿到结果（中途被预算/异常打断），必须留痕：
        # "调了没结果"和"根本没调"是两种故障，混成一种会误判成因。
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [_call("c1", "get_tasks")]},
        ]

        records = AgentLoop._extract_trajectory(messages)

        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["success"])
        self.assertIn("中断", records[0]["error"])

    def test_observation_without_call_is_kept(self):
        messages = [_observation("ghost", "get_tasks", {"success": True})]

        records = AgentLoop._extract_trajectory(messages)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tool_call_id"], "ghost")
        self.assertTrue(records[0]["success"])

    def test_broken_json_observation_is_flagged(self):
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [_call("c1", "t")]},
            {"role": "tool", "tool_call_id": "c1", "name": "t", "content": "not json"},
        ]

        records = AgentLoop._extract_trajectory(messages)

        self.assertFalse(records[0]["success"])
        self.assertIn("不是合法 JSON", records[0]["error"])
        # 原文仍留着：格式异常时原文比空值有用。
        self.assertEqual(records[0]["result"], "not json")

    def test_long_result_is_clipped_but_length_kept(self):
        limit = AgentLoop.TRAJECTORY_RESULT_CHARS
        payload = {"success": True, "summary": "ok", "data": {"blob": "x" * (limit + 500)}}
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [_call("c1", "t")]},
            _observation("c1", "t", payload),
        ]

        records = AgentLoop._extract_trajectory(messages)

        self.assertLessEqual(len(records[0]["result"]), limit + 20)
        self.assertIn("截断", records[0]["result"])
        # 原始长度单独记：只看到被截断的 result 时，得知道丢了多少。
        self.assertGreater(records[0]["result_chars"], limit)

    def test_turn_index_follows_execution_order(self):
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [_call("c1", "a")]},
            _observation("c1", "a", {"success": True}),
            {"role": "assistant", "content": None, "tool_calls": [_call("c2", "b")]},
            _observation("c2", "b", {"success": True}),
        ]

        records = AgentLoop._extract_trajectory(messages)

        self.assertEqual([item["turn_index"] for item in records], [0, 1])
        self.assertEqual([item["name"] for item in records], ["a", "b"])


class TrajectoryPersistenceTests(TestCase):
    def setUp(self):
        self.db = make_session()

    def tearDown(self):
        self.db.close()

    def test_roundtrip_preserves_order_and_fields(self):
        records = [
            {
                "turn_index": 0,
                "tool_call_id": "c1",
                "name": "get_tasks",
                "arguments": '{"status": "pending"}',
                "success": True,
                "summary": "查到 2 个",
                "error": None,
                "result": '{"success": true}',
                "result_chars": 16,
            },
            {
                "turn_index": 1,
                "tool_call_id": "c2",
                "name": "update_task",
                "arguments": '{"task_id": 1}',
                "success": False,
                "summary": None,
                "error": "task 1 not found",
                "result": "{}",
                "result_chars": 2,
            },
        ]
        chat_service.append_tool_calls(self.db, "s1", "run-1", records)
        self.db.commit()

        rows = chat_service.recent_tool_calls(self.db, "s1")

        self.assertEqual([row.name for row in rows], ["get_tasks", "update_task"])
        self.assertEqual([row.tool_call_id for row in rows], ["c1", "c2"])
        self.assertTrue(rows[0].success)
        self.assertFalse(rows[1].success)
        self.assertEqual(rows[1].error, "task 1 not found")

    def test_filter_by_run(self):
        chat_service.append_tool_calls(
            self.db,
            "s1",
            "run-1",
            [{"turn_index": 0, "tool_call_id": "a", "name": "x", "success": True}],
        )
        chat_service.append_tool_calls(
            self.db,
            "s1",
            "run-2",
            [{"turn_index": 0, "tool_call_id": "b", "name": "y", "success": True}],
        )
        self.db.commit()

        only_run_2 = chat_service.recent_tool_calls(self.db, "s1", agent_run_id="run-2")

        self.assertEqual([row.name for row in only_run_2], ["y"])

    def test_clearing_session_removes_trajectory(self):
        chat_service.append_message(self.db, "s1", "user", "你好")
        chat_service.append_tool_calls(
            self.db,
            "s1",
            "run-1",
            [{"turn_index": 0, "tool_call_id": "a", "name": "x", "success": True}],
        )
        self.db.commit()

        chat_service.clear_messages(self.db, "s1")
        self.db.commit()

        # 只清消息不清轨迹，等于"清空会话"后还能回看上一段调过什么。
        self.assertEqual(chat_service.recent_tool_calls(self.db, "s1"), [])
        self.assertEqual(chat_service.message_count(self.db, "s1"), 0)

    def test_other_sessions_are_untouched(self):
        chat_service.append_tool_calls(
            self.db,
            "s2",
            "run-1",
            [{"turn_index": 0, "tool_call_id": "a", "name": "x", "success": True}],
        )
        chat_service.append_message(self.db, "s1", "user", "你好")
        self.db.commit()

        chat_service.clear_messages(self.db, "s1")
        self.db.commit()

        self.assertEqual(len(chat_service.recent_tool_calls(self.db, "s2")), 1)


class TrajectoryFromRealLoopTests(TestCase):
    """跑一遍真实循环，确认轨迹确实被收集到 AgentResult 上。"""

    def setUp(self):
        self.time_patcher = patch(
            "app.services.time_policy.utcnow",
            return_value=datetime(2026, 9, 15),
        )
        self.time_patcher.start()
        self.db = make_session()
        self.loop = AgentLoop(
            llm=MockProvider(),
            registry=build_default_registry(),
            memory=Memory(),
            confirmation_store=PendingActionStore(),
            skill_registry=build_default_skills(),
        )

    def tearDown(self):
        self.db.close()
        self.time_patcher.stop()

    def test_loop_fills_trajectory(self):
        result = self.loop.run(self.db, "帮我看看现在有哪些任务", "s1")

        self.assertTrue(result.trajectory)
        self.assertTrue(result.tool_calls)
        # tool_calls 是给界面看的摘要，trajectory 保留协议字段，两者数量一致。
        self.assertEqual(len(result.trajectory), len(result.tool_calls))
        for record in result.trajectory:
            self.assertTrue(record["tool_call_id"])
            self.assertTrue(record["name"])
            self.assertIsNotNone(record["result"])

    def test_trajectory_does_not_leak_into_next_prompt(self):
        # 轨迹落库但不能进上下文：历史工具返回值再喂给模型，会挤占预算，
        # 还会诱导它把过时的查询结果当成当前事实。
        self.loop.run(self.db, "帮我看看现在有哪些任务", "s1")
        chat_service.append_message(self.db, "s1", "user", "第一次")
        chat_service.append_message(self.db, "s1", "assistant", "好的")
        self.db.commit()

        messages = self.loop._build_messages(self.db, "s1", "继续", None)

        self.assertTrue(all(item.get("role") != "tool" for item in messages))


class TrajectoryRouteTests(TestCase):
    def test_route_is_registered(self):
        from app.main import app

        # 用 OpenAPI 而不是遍历 app.routes：后者里有 _IncludedRouter 这类中间对象。
        paths = set(app.openapi()["paths"])
        self.assertIn("/api/agent/sessions/{session_id}/tool-calls", paths)
