# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：test_context_compaction.py
"""上下文压缩：token 估算、语义化裁剪、各级预算。

旧实现有两处粗糙：一是所有阈值都是字符数，同一段预算在中文和 JSON 上差三倍；
二是裁剪只看位置（首 1500 + 尾 300），任务清单的中间条目会被整段丢掉，而排期恰恰
依赖中间那些。这里锁住新行为：按 token 计、按信息密度取舍、压缩留痕。
"""

import json
from unittest import TestCase

from app.agent.compaction import (
    compact_payload,
    compress_text,
    degrade_tool_message,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_tokens,
    prepare_history,
)
from app.agent.confirmation import PendingActionStore
from app.agent.loop import AgentLoop
from app.agent.memory import Memory
from app.llm.mock import MockProvider
from app.tools.defaults import build_default_registry
from tests.helpers import make_session


class EstimateTokensTests(TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(estimate_tokens(""), 0)

    def test_chinese_is_roughly_one_token_per_char(self):
        text = "今天下午安排一次论文写作"  # 12 个中文
        tokens = estimate_tokens(text)
        self.assertGreaterEqual(tokens, len(text))
        self.assertLessEqual(tokens, len(text) * 1.3)

    def test_ascii_is_cheaper_per_char(self):
        text = "a" * 350
        tokens = estimate_tokens(text)
        self.assertLess(tokens, 150)

    def test_chinese_costs_more_than_ascii_of_same_length(self):
        self.assertGreater(estimate_tokens("安" * 200), estimate_tokens("a" * 200))

    def test_message_tokens_counts_tool_calls(self):
        plain = {"role": "assistant", "content": "好的"}
        with_calls = {
            "role": "assistant",
            "content": "好的",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_tasks", "arguments": "{}"}}
            ],
        }
        self.assertGreater(
            estimate_message_tokens(with_calls), estimate_message_tokens(plain)
        )


class CompressTextTests(TestCase):
    def test_short_text_is_untouched(self):
        text = "明天上午十点开会"
        self.assertEqual(compress_text(text, 500), text)

    def test_constraint_line_survives_while_filler_is_dropped(self):
        filler = [f"备注第{i}条：这是一段没有信息量的填充文本，用来占位置。" for i in range(40)]
        lines = filler[:20] + ["关键约束：周三之前不能排任何会议，截止 2026-11-01。"] + filler[20:]
        text = "\n".join(["会议纪要开始"] + lines + ["以上。"])

        compressed = compress_text(text, max_tokens=260)

        self.assertIn("周三之前不能排任何会议", compressed)
        self.assertIn("会议纪要开始", compressed)  # 首段必留
        self.assertIn("以上。", compressed)  # 尾段必留
        self.assertIn("已压缩", compressed)
        self.assertLessEqual(estimate_tokens(compressed), 260)

    def test_output_never_exceeds_budget(self):
        text = "\n".join(f"第{i}行：安排任务 {i}，时长 {i * 5} 分钟" for i in range(300))
        for budget in (100, 300, 800):
            self.assertLessEqual(estimate_tokens(compress_text(text, budget)), budget)

    def test_single_long_line_falls_back_to_middle_truncation(self):
        compressed = compress_text("A" * 20000, max_tokens=300)
        self.assertIn("较长历史内容已压缩", compressed)
        self.assertLessEqual(estimate_tokens(compressed), 300)

    def test_empty_text(self):
        self.assertEqual(compress_text("", 100), "")


class CompactPayloadTests(TestCase):
    def test_empty_values_are_dropped(self):
        compacted = compact_payload(
            {"id": 7, "title": "写论文", "description": None, "notes": "", "tags": []}
        )
        self.assertEqual(compacted, {"id": 7, "title": "写论文"})

    def test_long_list_keeps_head_and_tail_with_count(self):
        items = [{"id": index, "title": f"任务{index}"} for index in range(60)]
        compacted = compact_payload({"tasks": items})

        kept = compacted["tasks"]
        self.assertEqual(kept[0]["id"], 0)  # 头
        self.assertEqual(kept[-1]["id"], 59)  # 尾
        omitted = [item for item in kept if "omitted_count" in item]
        self.assertEqual(len(omitted), 1)
        self.assertEqual(omitted[0]["omitted_count"], 60 - 12 - 4)

    def test_route_path_is_folded_to_endpoints(self):
        path = [{"longitude": index} for index in range(100)]
        compacted = compact_payload({"routes": [{"path": path}]})

        folded = compacted["routes"][0]["path"]
        self.assertEqual(folded["point_count"], 100)
        self.assertEqual(folded["start"]["longitude"], 0)
        self.assertEqual(folded["end"]["longitude"], 99)

    def test_long_string_is_compressed(self):
        text = "\n".join(f"第{i}条记录，包含一些说明文字。" for i in range(200))
        compacted = compact_payload({"raw": text})
        self.assertLessEqual(estimate_tokens(compacted["raw"]), 400)

    def test_deep_nesting_is_cut(self):
        payload = current = {}
        for _ in range(12):
            current["next"] = {}
            current = current["next"]
        current["value"] = "底部"
        self.assertIn("嵌套内容已省略", json.dumps(compact_payload(payload), ensure_ascii=False))


class DegradeToolMessageTests(TestCase):
    def test_data_is_dropped_summary_kept(self):
        message = {
            "role": "tool",
            "tool_call_id": "c1",
            "name": "get_tasks",
            "content": json.dumps(
                {"success": True, "summary": "查到 3 个任务", "data": {"tasks": [1, 2, 3]}},
                ensure_ascii=False,
            ),
        }
        degraded = degrade_tool_message(message)

        payload = json.loads(degraded["content"])
        self.assertIsNone(payload["data"])
        self.assertTrue(payload["data_omitted"])
        self.assertEqual(payload["summary"], "查到 3 个任务")
        # 协议字段必须原样保留，否则后续轮次会对不上 tool_call_id
        self.assertEqual(degraded["tool_call_id"], "c1")
        self.assertEqual(degraded["name"], "get_tasks")
        self.assertEqual(degraded["role"], "tool")

    def test_already_degraded_returns_none(self):
        message = {
            "role": "tool",
            "content": json.dumps({"success": True, "data_omitted": True}),
        }
        self.assertIsNone(degrade_tool_message(message))

    def test_non_json_content_returns_none(self):
        self.assertIsNone(degrade_tool_message({"role": "tool", "content": "纯文本"}))


class PrepareHistoryTests(TestCase):
    def test_oldest_messages_are_dropped_over_budget(self):
        messages = [
            {"role": "assistant", "content": f"第{index}条历史" + "填充" * 200}
            for index in range(10)
        ]
        kept, dropped = prepare_history(messages, 1200, 1500)

        self.assertGreater(dropped, 0)
        self.assertEqual(len(kept), len(messages) - dropped)
        # 保留的是最新的若干条
        self.assertEqual(kept[-1]["content"], messages[-1]["content"])

    def test_single_oversized_message_is_still_kept(self):
        messages = [{"role": "user", "content": "重要约束" * 2000}]
        kept, dropped = prepare_history(messages, 1200, 100)

        self.assertEqual(dropped, 0)
        self.assertEqual(len(kept), 1)


class LoopBudgetTests(TestCase):
    def setUp(self):
        self.db = make_session()
        self.loop = AgentLoop(
            llm=MockProvider(),
            registry=build_default_registry(),
            memory=Memory(),
            confirmation_store=PendingActionStore(),
        )

    def _tool_message(self, call_id, items):
        return self.loop._tool_message(
            call_id, "get_tasks", True, data={"tasks": items}, summary="查到任务"
        )

    def test_biggest_tool_result_is_degraded_first(self):
        small = self._tool_message("c1", [{"id": 1, "title": "小结果"}])
        big = self._tool_message("c2", [{"id": i, "title": "大结果" + "x" * 40} for i in range(20)])
        messages = [
            {"role": "system", "content": "规则" * 50},
            {"role": "user", "content": "把任务排一下"},
            small,
            big,
        ]
        self.loop.PROMPT_TOKEN_BUDGET = estimate_messages_tokens(messages) - 60

        total = self.loop._enforce_prompt_budget(messages)

        self.assertLessEqual(total, self.loop.PROMPT_TOKEN_BUDGET)
        # 大的先被降级，小的保持原样（降级是替换列表元素，不是原地改）
        self.assertTrue(json.loads(messages[3]["content"]).get("data_omitted"))
        self.assertFalse(json.loads(messages[2]["content"]).get("data_omitted"))

    def test_budget_is_noop_when_under_limit(self):
        messages = [{"role": "user", "content": "你好"}]
        before = json.dumps(messages, ensure_ascii=False)
        self.loop._enforce_prompt_budget(messages)
        self.assertEqual(json.dumps(messages, ensure_ascii=False), before)

    def test_dropped_history_is_announced_in_system_prompt(self):
        self.loop.HISTORY_TOKEN_BUDGET = 60
        for index in range(6):
            self.loop.memory.add("s1", "user", f"第{index}轮：" + "很长的历史内容" * 60)

        messages = self.loop._build_messages(self.db, "s1", "继续")
        system = messages[0]["content"]

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("已省略", system)
        self.assertIn("用工具重新查询", system)

    def test_history_never_exceeds_budget(self):
        self.loop.HISTORY_TOKEN_BUDGET = 300
        for index in range(8):
            self.loop.memory.add("s2", "assistant", f"回复{index}：" + "内容" * 400)

        messages = self.loop._build_messages(self.db, "s2", "继续")
        history = messages[1:]

        self.assertLessEqual(
            sum(estimate_message_tokens(item) for item in history),
            self.loop.HISTORY_TOKEN_BUDGET,
        )
