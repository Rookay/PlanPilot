# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：test_skill_selection.py
"""模型自选场景（select_skill）的行为测试。

覆盖四类风险：
1. 关键词未命中时，模型能自选并让新场景的指引进入后续上下文；
2. 切换后系统提示词里的旧场景不会残留成主指引；
3. 切到有白名单的场景后，切换工具本身仍然可见（否则无法切回）；
4. 切换不占用业务工具预算，但有独立次数上限。
"""

from unittest import TestCase

from app.agent.confirmation import PendingActionStore
from app.agent.loop import AgentLoop
from app.agent.memory import Memory
from app.llm.mock import MockProvider
from app.skills.planner_skills import build_default_skills
from app.tools.defaults import build_default_registry
from tests.helpers import make_session


class _RecordingProvider(MockProvider):
    """记录每次请求的 messages / tools，便于断言上下文真的变了。"""

    def __init__(self, script=None):
        super().__init__(script=script)
        self.requests = []

    def chat(self, messages, tools=None):
        self.requests.append(
            {
                "messages": [dict(item) for item in messages],
                "tool_names": [
                    item.get("function", {}).get("name") for item in (tools or [])
                ],
            }
        )
        return super().chat(messages, tools)


def _build_loop(llm) -> AgentLoop:
    skills = build_default_skills()
    return AgentLoop(
        llm=llm,
        registry=build_default_registry(skills),
        memory=Memory(),
        confirmation_store=PendingActionStore(),
        skill_registry=skills,
    )


def _switch_turn(name: str, reason: str = "") -> dict:
    return {"tool_calls": [{"name": "select_skill", "arguments": {"name": name, "reason": reason}}]}


class ModelSelectsSkillTests(TestCase):
    def setUp(self):
        self.db = make_session()

    def tearDown(self):
        self.db.close()

    def test_model_switches_when_keywords_miss(self):
        # "处理一下这个" 不含任何高置信业务词，关键词不会命中。
        provider = _RecordingProvider(
            script=[_switch_turn("replanning", "用户要调整已有安排"), {"text": "好的，我按调整处理。"}]
        )
        loop = _build_loop(provider)

        result = loop.run(self.db, "处理一下这个", "s-switch")

        self.assertEqual(result.selected_skill, "replanning")
        self.assertEqual(result.skill_source, "model")
        # 第二轮请求里必须出现新场景的指引。
        second_system = " ".join(
            str(item.get("content"))
            for item in provider.requests[1]["messages"]
            if item.get("role") == "system"
        )
        self.assertIn("【场景已切换】", second_system)
        self.assertIn("【重规划】", second_system)

    def test_keyword_hit_marks_source_as_keyword(self):
        provider = _RecordingProvider(script=[{"text": "我先看看冲突。"}])
        loop = _build_loop(provider)

        result = loop.run(self.db, "明天下午有没有时间冲突", "s-keyword")

        self.assertEqual(result.selected_skill, "conflict_resolution")
        self.assertEqual(result.skill_source, "keyword")

    def test_no_skill_uses_general_mode(self):
        provider = _RecordingProvider(script=[{"text": "通用回答。"}])
        loop = _build_loop(provider)

        result = loop.run(self.db, "看看任务", "s-general")

        self.assertIsNone(result.selected_skill)
        self.assertEqual(result.skill_source, "none")
        first_system = " ".join(
            str(item.get("content"))
            for item in provider.requests[0]["messages"]
            if item.get("role") == "system"
        )
        self.assertIn("未匹配到预设场景", first_system)
        self.assertIn("select_skill", first_system)

    def test_control_tool_stays_visible_after_whitelist_narrowing(self):
        provider = _RecordingProvider(
            script=[_switch_turn("plan_alternatives"), {"text": "给你三套方案。"}]
        )
        loop = _build_loop(provider)

        loop.run(self.db, "看看任务", "s-whitelist")

        # plan_alternatives 配了白名单；切换后工具集应收窄，但 select_skill 必须保留。
        tool_names = provider.requests[1]["tool_names"]
        self.assertIn("select_skill", tool_names)
        self.assertIn("compare_plan_alternatives", tool_names)
        self.assertNotIn("delete_events", tool_names)
        self.assertNotIn("generate_plan", tool_names)

    def test_unknown_skill_is_rejected_without_switching(self):
        provider = _RecordingProvider(
            script=[_switch_turn("not_a_skill"), {"text": "无法切换。"}]
        )
        loop = _build_loop(provider)

        result = loop.run(self.db, "看看任务", "s-unknown")

        self.assertIsNone(result.selected_skill)
        switch_logs = [item for item in result.tool_calls if item["name"] == "select_skill"]
        self.assertEqual(len(switch_logs), 1)
        self.assertFalse(switch_logs[0]["success"])
        # schema 的 enum 在工具执行前就拦下了无效场景名，报错里会列出全部合法值。
        self.assertIn("不合法", switch_logs[0]["summary"])
        self.assertIn("not_a_skill", switch_logs[0]["summary"])
        self.assertIn("conflict_resolution", switch_logs[0]["summary"])

    def test_repeated_switch_reports_already_active(self):
        provider = _RecordingProvider(
            script=[_switch_turn("conflict_resolution"), {"text": "继续看冲突。"}]
        )
        loop = _build_loop(provider)

        result = loop.run(self.db, "明天下午有没有时间冲突", "s-repeat")

        self.assertEqual(result.selected_skill, "conflict_resolution")
        # 关键词已经选中，模型再选同一个应被识别为重复，而非改写来源。
        self.assertEqual(result.skill_source, "keyword")
        switch_logs = [item for item in result.tool_calls if item["name"] == "select_skill"]
        self.assertIn("无需重复切换", switch_logs[0]["summary"])

    def test_switching_does_not_consume_business_budget(self):
        provider = _RecordingProvider(
            script=[
                _switch_turn("replanning"),
                _switch_turn("travel_planning"),
                _switch_turn("weather_planning"),
                {"text": "已按天气场景处理。"},
            ]
        )
        loop = _build_loop(provider)

        result = loop.run(self.db, "处理一下这个", "s-budget")

        # 三次切换都成功，且没有被 24 次业务预算限制。
        switch_logs = [item for item in result.tool_calls if item["name"] == "select_skill"]
        self.assertEqual(len(switch_logs), 3)
        self.assertTrue(all(item["success"] for item in switch_logs))
        self.assertEqual(result.selected_skill, "weather_planning")

    def test_switch_count_is_capped(self):
        provider = _RecordingProvider(
            script=[
                _switch_turn("replanning"),
                _switch_turn("travel_planning"),
                _switch_turn("weather_planning"),
                _switch_turn("task_understanding"),
                {"text": "不再切换。"},
            ]
        )
        loop = _build_loop(provider)

        result = loop.run(self.db, "处理一下这个", "s-cap")

        switch_logs = [item for item in result.tool_calls if item["name"] == "select_skill"]
        self.assertEqual(len(switch_logs), 4)
        self.assertFalse(switch_logs[-1]["success"])
        self.assertTrue(switch_logs[-1].get("limited"))
        # 达到上限后保持上一次成功的场景。
        self.assertEqual(result.selected_skill, "weather_planning")
