# -*- coding: UTF-8 -*-
"""``build_world_summary`` 降级行为测试。

``build_world_summary`` 是 Agent 每次请求时构造 system prompt 必经路径。
如果里面任意一段 DB 查询抛异常而没有兜底，整条 Agent loop 就挂了——用户看到
的是 500 而不是"我现在无法访问数据库"。

修法后应当：
- 单段失败：降级显示 ``（暂不可用）``，logger.warning，函数返回字符串（不抛）
- 多段失败：互不影响，各自降级
- 全部失败（极端）：函数顶层 still 返回只含 UTC 时间的最小快照
"""

from unittest import TestCase
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from app.agent.context import build_world_summary
from tests.helpers import make_session


class WorldSummaryFallbackTests(TestCase):
    def setUp(self):
        self.db = make_session()

    # 几个段落标题常量，降级文案总是形如"标题：（暂不可用）"
    _SECTIONS = (
        "规划偏好",
        "常用地点",
        "待办任务",
        "近期固定日程",
        "近期任务安排",
    )

    def _degraded_section(self, snapshot):
        """返回快照中所有触发降级的段落名（"规划偏好" 等）。"""

        degraded = []
        for section in self._SECTIONS:
            if f"{section}：（暂不可用）" in snapshot:
                degraded.append(section)
        return degraded

    def test_normal_db_returns_full_snapshot(self):
        """正常数据库应输出全 5 段，而非降级文案。"""
        snapshot = build_world_summary(self.db)
        # 完整段落标题都要出现
        for section in self._SECTIONS:
            self.assertIn(f"{section}：", snapshot)
        self.assertEqual(self._degraded_section(snapshot), [])

    def test_preferences_failure_marks_only_that_section(self):
        with patch(
            "app.agent.context.preference_service.get_preferences",
            side_effect=OperationalError("select", {}, Exception("db down")),
        ):
            with self.assertLogs("app.agent.context", level="WARNING") as captured:
                snapshot = build_world_summary(self.db)

        self.assertEqual(self._degraded_section(snapshot), ["规划偏好"])
        joined = "\n".join(captured.output)
        self.assertIn("加载规划偏好失败", joined)

    def test_locations_failure_marks_only_that_section(self):
        with patch(
            "app.agent.context.location_service.list_locations",
            side_effect=OperationalError("select", {}, Exception("boom")),
        ):
            with self.assertLogs("app.agent.context", level="WARNING") as captured:
                snapshot = build_world_summary(self.db)

        self.assertEqual(self._degraded_section(snapshot), ["常用地点"])
        joined = "\n".join(captured.output)
        self.assertIn("加载常用地点失败", joined)

    def _faulty_query_for(self, model):
        """返回一个会抛异常的 db.query 替代品，仅对指定 model 炸。"""

        from app.agent import context as ctx_mod

        original_query = self.db.query

        def faulty_query(*args, **kwargs):
            if args and args[0] is model:
                raise OperationalError("select", {}, Exception(f"{model.__name__} boom"))
            return original_query(*args, **kwargs)

        return faulty_query

    def test_task_query_failure_marks_only_that_section(self):
        from app.models.task import Task

        faulty = self._faulty_query_for(Task)
        original = self.db.query
        self.db.query = faulty
        try:
            with self.assertLogs("app.agent.context", level="WARNING") as captured:
                snapshot = build_world_summary(self.db)
        finally:
            self.db.query = original

        self.assertEqual(self._degraded_section(snapshot), ["待办任务"])
        joined = "\n".join(captured.output)
        self.assertIn("加载待办任务失败", joined)

    def test_event_query_failure_marks_only_that_section(self):
        from app.models.event import Event

        faulty = self._faulty_query_for(Event)
        original = self.db.query
        self.db.query = faulty
        try:
            with self.assertLogs("app.agent.context", level="WARNING") as captured:
                snapshot = build_world_summary(self.db)
        finally:
            self.db.query = original

        self.assertEqual(self._degraded_section(snapshot), ["近期固定日程"])
        joined = "\n".join(captured.output)
        self.assertIn("加载固定日程失败", joined)

    def test_schedule_query_failure_marks_only_that_section(self):
        from app.models.schedule import Schedule

        faulty = self._faulty_query_for(Schedule)
        original = self.db.query
        self.db.query = faulty
        try:
            with self.assertLogs("app.agent.context", level="WARNING") as captured:
                snapshot = build_world_summary(self.db)
        finally:
            self.db.query = original

        self.assertEqual(self._degraded_section(snapshot), ["近期任务安排"])
        joined = "\n".join(captured.output)
        self.assertIn("加载任务安排失败", joined)

    def test_top_level_fallback_when_render_itself_fails(self):
        """极端：连 ``utcnow`` 都炸（极不可能），顶层 try/except 仍要返回字符串。"""

        with patch("app.agent.context.utcnow", side_effect=RuntimeError("now boom")):
            with self.assertLogs("app.agent.context", level="ERROR") as captured:
                snapshot = build_world_summary(self.db)

        self.assertIsInstance(snapshot, str)
        self.assertTrue(len(snapshot) > 0)
        self.assertIn("当前时间快照暂不可用", snapshot)
        joined = "\n".join(captured.output)
        self.assertIn("顶层兜底", joined)
        self.assertIn("Traceback", joined)
