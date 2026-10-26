# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：test_skill_routing.py
"""Skill 关键词路由的回归测试。

关键词只做"高置信快捷方式"：明确业务词直接命中，泛词（安排 / 规划 / 今天 / 明天）
故意不收录，未命中的请求交给模型通过 select_skill 自选。本文件锁住这两条契约，
避免以后往词表里加泛词时把请求抢回错误场景。
"""

from unittest import TestCase

from app.skills.planner_skills import build_default_skills


class SkillRoutingTests(TestCase):
    def setUp(self):
        self.registry = build_default_skills()

    def _select(self, message: str):
        return self.registry.select(message)

    def assertSelects(self, message: str, expected: str):
        skill = self._select(message)
        self.assertIsNotNone(skill, f"期望命中 {expected}，实际未命中：{message}")
        self.assertEqual(skill.name, expected, f"消息：{message}")

    def test_travel_request_with_generic_verb_goes_to_travel(self):
        # "安排"曾经抢走这条请求，导致旅行被送进日程规划。
        self.assertSelects("安排马鞍山第一天行程", "travel_planning")

    def test_travel_request_with_multiple_anchors(self):
        self.assertSelects(
            "9月27日去马鞍山玩，先去酒店放行李，安排第一天行程",
            "travel_planning",
        )

    def test_trip_revision_uses_replanning(self):
        self.assertSelects("把这一天整体顺延一小时", "replanning")

    def test_trip_batch_delete_uses_travel(self):
        self.assertSelects("清空上海行程和出去玩", "travel_planning")

    def test_conflict_check_is_not_hijacked_by_day_words(self):
        # "明天"曾经把冲突检查抢到每日规划。
        self.assertSelects("明天下午有没有时间冲突", "conflict_resolution")

    def test_date_comparison(self):
        self.assertSelects("下周我想开车回家，哪天比较合适", "date_recommendation")

    def test_plan_health(self):
        self.assertSelects("看看我这周的计划有没有风险，任务来得及吗", "plan_health")

    def test_plan_alternatives(self):
        self.assertSelects("给我几套方案对比一下，不要直接应用", "plan_alternatives")

    def test_weather(self):
        self.assertSelects("明天会下雨吗，户外安排要不要改", "weather_planning")

    def test_generic_verbs_do_not_match(self):
        # 这些消息只含泛词，必须交给模型自选，而不是被规则乱猜。
        for message in (
            "论文比较急，尽快安排，其余的安排到后面，留出空闲",
            "看看任务",
            "撤销刚才的计划",
            "现在适合做什么",
            "这周先不用安排了",
        ):
            with self.subTest(message=message):
                self.assertIsNone(self._select(message))

    def test_negation_is_not_matched_by_generic_verb(self):
        # 否定句里出现"安排"不应触发排期场景。
        self.assertIsNone(self._select("这周先不用安排了"))

    def test_multi_intent_returns_one_skill(self):
        # 只取一个场景：复合请求由模型在切换工具里纠正。
        skill = self._select("先看看明天有什么，再把论文往后挪")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.name, "replanning")

    def test_lookup_by_name(self):
        skill = self.registry.get("travel_planning")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.description, "规划有明确日期、酒店和多地点路线的旅行行程")
        self.assertIsNone(self.registry.get("not_a_skill"))

    def test_registry_lists_all_skills(self):
        names = self.registry.names()
        self.assertEqual(len(names), 10)
        # select_skill 是工具入口，不是业务场景，不应出现在场景清单里。
        self.assertNotIn("select_skill", names)
        self.assertEqual(len(set(names)), len(names))
