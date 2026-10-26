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
from app.services import chat_service
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


class CompressStructuredTextTests(TestCase):
    """单行 JSON 的压缩必须保住结构。

    上一版对单行文本一律走"保头保尾"硬截断，压出来的 JSON 缺引号缺括号，
    连 ``json.loads`` 都过不了；改成按顶层逗号切段后又有新问题——外层往往
    只有三五个 key，体积最大的那个数据字段会被当成"塞不下的段"整段丢掉，
    最后只剩 success / summary 这类元信息。这里锁住真正的行为：走结构化
    瘦身，逐档收紧列表，输出始终可解析。
    """

    @staticmethod
    def _payload(count: int = 30) -> str:
        items = [
            {
                "id": index,
                "title": f"任务{index}",
                "start": f"2026-11-0{index % 9 + 1}T0{index % 9}:00:00Z",
            }
            for index in range(count)
        ]
        return json.dumps(
            {"success": True, "summary": f"查到 {count} 个任务", "data": {"tasks": items}},
            ensure_ascii=False,
        )

    def test_output_is_still_valid_json(self):
        raw = self._payload()
        compressed = compress_text(raw, 300)

        parsed = json.loads(compressed)  # 不合法会直接抛，这就是断言
        self.assertIn("data", parsed)

    def test_data_field_is_not_dropped(self):
        raw = self._payload()
        parsed = json.loads(compress_text(raw, 300))

        tasks = parsed["data"]["tasks"]
        self.assertGreater(len(tasks), 1)
        # 头尾都要留：开头是有序的早期条目，结尾通常是最新的一条
        self.assertEqual(tasks[0]["id"], 0)
        self.assertEqual(tasks[-1]["id"], 29)
        self.assertTrue(any(item.get("omitted_count") for item in tasks))

    def test_tighter_budget_keeps_shrinking_but_stays_valid(self):
        raw = self._payload(60)
        for budget in (400, 200, 90):
            compressed = compress_text(raw, budget)
            parsed = json.loads(compressed)
            self.assertIn("data", parsed)
            self.assertLessEqual(estimate_tokens(compressed), budget)

    def test_json_nested_in_string_field_degrades_gracefully(self):
        """收紧到只剩骨架仍然超预算时，退回硬截断而不是产出坏 JSON。

        这是一条明确的取舍：宁可让模型看到"这里被截断了"的纯文本，
        也不要让它拿到一段看似完整、实际解析失败的 JSON。
        """
        raw = json.dumps({"k": "值" * 4000}, ensure_ascii=False)
        compressed = compress_text(raw, 40)

        self.assertLessEqual(estimate_tokens(compressed), 40)


class EdgeSegmentTests(TestCase):
    """首尾段只有位置加成，没有豁免权。"""

    def test_edges_are_kept_when_budget_allows(self):
        segments = ["开场白。"] + [f"第{index}项：普通内容。" for index in range(30)] + ["以上。"]
        compressed = compress_text("\n".join(segments), 240)

        self.assertIn("开场白", compressed)
        self.assertIn("以上", compressed)

    def test_edges_can_be_dropped_when_budget_is_tiny(self):
        """预算只够一段时，硬保首尾会把结论挤掉。这里只要求"不超预算且留痕"。"""
        segments = ["开场白" + "很长的铺垫" * 40] + [
            f"第{index}项：普通内容。" for index in range(30)
        ] + ["结论：必须锁定周三下午。"]

        compressed = compress_text("\n".join(segments), 120)

        self.assertLessEqual(estimate_tokens(compressed), 120)
        self.assertIn("必须锁定", compressed)  # 约束句优先于位置加成

    def test_cut_point_is_aligned_to_punctuation(self):
        from app.agent.compaction import _align_cut

        text = "甲乙丙。丁戊己"
        self.assertEqual(_align_cut(text, 5), 4)  # 退到「。」之后
        self.assertEqual(_align_cut(text, 3), 3)  # 窗口内没有标点就原样返回

    def test_middle_truncation_does_not_cut_inside_a_sentence(self):
        text = "".join(f"第{index}句话的内容在这里。" for index in range(200))
        compressed = compress_text(text, 120)

        self.assertLessEqual(estimate_tokens(compressed), 120)
        self.assertIn("较长历史内容已压缩", compressed)
        # 标记前面应当是完整的一句，而不是半截
        head = compressed.split("...", 1)[0]
        self.assertTrue(head.endswith("。"), head[-12:])


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
            chat_service.append_message(
                self.db, "s1", "user", f"第{index}轮：" + "很长的历史内容" * 60
            )
        self.db.commit()

        messages = self.loop._build_messages(self.db, "s1", "继续")
        system = messages[0]["content"]

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("已省略", system)
        self.assertIn("用工具重新查询", system)

    def test_history_never_exceeds_budget(self):
        self.loop.HISTORY_TOKEN_BUDGET = 300
        for index in range(8):
            chat_service.append_message(
                self.db, "s2", "assistant", f"回复{index}：" + "内容" * 400
            )
        self.db.commit()

        messages = self.loop._build_messages(self.db, "s2", "继续")
        # 末条是本轮用户消息，不占历史预算；历史部分要落在窗口与 token 闸门之内。
        history = messages[1:-1]
        self.assertEqual(messages[-1], {"role": "user", "content": "继续"})

        self.assertLessEqual(
            sum(estimate_message_tokens(item) for item in history),
            self.loop.HISTORY_TOKEN_BUDGET,
        )

    def test_history_is_read_from_database_not_process_cache(self):
        """同一 session 换一个循环实例也要看得到历史——旧的进程内字典做不到。"""
        chat_service.append_message(self.db, "s3", "user", "周六上午去南京")
        chat_service.append_message(self.db, "s3", "assistant", "好的，已记下")
        self.db.commit()

        fresh_loop = AgentLoop(
            llm=MockProvider(),
            registry=build_default_registry(),
            memory=Memory(),
            confirmation_store=PendingActionStore(),
        )
        messages = fresh_loop._build_messages(self.db, "s3", "把地点改成苏州")

        contents = [item["content"] for item in messages]
        self.assertIn("周六上午去南京", contents)
        self.assertIn("好的，已记下", contents)
        self.assertEqual(messages[-1]["content"], "把地点改成苏州")
