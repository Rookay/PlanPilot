# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：test_tool_argument_parsing.py
"""工具参数解析：区分“语法错”和“没填字段”。

模型返回的 ``arguments`` 是一段文本，需要自己解析。历史上解析失败会退化成
空字典，于是模型收到的反馈变成“缺字段”，它会去补字段而不是修语法；参数全
可选的工具还会静默执行成一次宽松查询。这里的用例锁住新的行为：
解析失败保留独立类别，并且不执行工具。
"""

from unittest import TestCase

from app.agent.confirmation import PendingActionStore
from app.agent.loop import AgentLoop
from app.agent.memory import Memory
from app.llm.base import LLMProvider, LLMResponse, ToolCall
from app.llm.openai_compat import _parse_arguments
from app.models.task import Task
from app.tools.defaults import build_default_registry
from tests.helpers import make_session


class ParseArgumentsTests(TestCase):
    """纯函数层：四类输入各自的结果。"""

    def test_missing_arguments_is_not_an_error(self):
        self.assertEqual(_parse_arguments("get_tasks", None), ({}, None))

    def test_empty_string_is_not_an_error(self):
        self.assertEqual(_parse_arguments("get_tasks", ""), ({}, None))
        self.assertEqual(_parse_arguments("get_tasks", "   "), ({}, None))

    def test_valid_object_parses(self):
        args, error = _parse_arguments("get_tasks", '{"q": "论文"}')
        self.assertEqual(args, {"q": "论文"})
        self.assertIsNone(error)

    def test_dict_is_accepted_directly(self):
        args, error = _parse_arguments("get_tasks", {"q": "论文"})
        self.assertEqual(args, {"q": "论文"})
        self.assertIsNone(error)

    def test_trailing_comma_keeps_syntax_error_category(self):
        args, error = _parse_arguments("create_task", '{"title": "写论文",}')
        self.assertEqual(args, {})
        self.assertIsNotNone(error)
        self.assertIn("不是合法 JSON", error)
        # 必须点名工具，并明确要求重写整段，而不是补字段
        self.assertIn("create_task", error)
        self.assertIn("重新输出完整", error)

    def test_array_is_rejected(self):
        args, error = _parse_arguments("get_tasks", '["论文"]')
        self.assertEqual(args, {})
        self.assertIn("必须是 JSON 对象", error)

    def test_non_dict_scalar_is_rejected(self):
        args, error = _parse_arguments("get_tasks", '"论文"')
        self.assertEqual(args, {})
        self.assertIn("必须是 JSON 对象", error)

    def test_long_raw_text_is_truncated(self):
        raw = '{"title": "' + "长" * 400 + '",}'
        _, error = _parse_arguments("create_task", raw)
        self.assertIn("原文共", error)
        self.assertLess(len(error), 700)


class _ScriptedProvider(LLMProvider):
    """按脚本依次返回响应，便于驱动多轮循环。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.chat_calls = 0

    def chat(self, messages, tools=None):
        self.chat_calls += 1
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(text="已完成")

    def structured(self, messages, output_schema):
        raise NotImplementedError


def _bad_call(index: int, message: str = "create_task 的参数不是合法 JSON。"):
    return ToolCall(
        id=f"call_{index}",
        name="create_task",
        arguments={},
        parse_error=message,
    )


class AgentLoopArgumentErrorTests(TestCase):
    """循环层：解析失败不执行工具，且连续失败会止损。"""

    def setUp(self):
        self.db = make_session()

    def _build_loop(self, provider):
        return AgentLoop(
            llm=provider,
            registry=build_default_registry(),
            memory=Memory(),
            confirmation_store=PendingActionStore(),
            skill_registry=None,
        )

    def test_parse_error_is_reported_not_executed(self):
        provider = _ScriptedProvider(
            [
                LLMResponse(tool_calls=[_bad_call(1)]),
                LLMResponse(text="参数格式有问题，我需要重新生成"),
            ]
        )
        result = self._build_loop(provider).run(self.db, "创建一个任务", "s1")

        self.assertEqual(result.reply, "参数格式有问题，我需要重新生成")
        self.assertEqual(len(result.tool_calls), 1)
        entry = result.tool_calls[0]
        self.assertTrue(entry["argument_error"])
        self.assertFalse(entry["success"])
        self.assertIn("不是合法 JSON", entry["summary"])
        # 工具没有被执行：任务表仍然为空
        self.assertEqual(self.db.query(Task).count(), 0)
        self.assertEqual(result.applied_changes, [])

    def test_two_parse_errors_stop_early(self):
        provider = _ScriptedProvider(
            [
                LLMResponse(tool_calls=[_bad_call(1)]),
                LLMResponse(tool_calls=[_bad_call(2)]),
                LLMResponse(text="我无法生成合法参数"),
            ]
        )
        result = self._build_loop(provider).run(self.db, "创建一个任务", "s1")

        # 两轮失败 + 一次无工具总结
        self.assertEqual(provider.chat_calls, 3)
        self.assertEqual(len(result.tool_calls), 2)
        self.assertIn("无法生成合法参数", result.reply)
        self.assertEqual(self.db.query(Task).count(), 0)

    def test_parse_error_counts_toward_call_budget(self):
        provider = _ScriptedProvider([LLMResponse(tool_calls=[_bad_call(1)])] * 3)
        loop = self._build_loop(provider)
        loop.MAX_ARGUMENT_PARSE_ERRORS = 99  # 关掉止损，单独观察预算计数
        result = loop.run(self.db, "创建一个任务", "s1")

        self.assertEqual(len(result.tool_calls), 3)
        self.assertTrue(all(item["argument_error"] for item in result.tool_calls))
