# -*- coding: UTF-8 -*-
"""``AgentDatabaseSnapshot`` 行为测试。

这个文件守住的不只是当前实现，还守住一个隐性的架构契约——所有 Agent 工具
在执行期间不得自 commit。测试通过 ``ToolCommitNotAllowedError`` 的存在性声明
该约束；snapshot 自身的 capture/restore round-trip 行为也写在这里。
"""

from unittest import TestCase

from app.agent.transaction import (
    AgentDatabaseSnapshot,
    ToolCommitNotAllowedError,
    _DELETE_ORDER,
    _MODELS,
    _RESTORE_ORDER,
)
from app.models.task import Task
from tests.helpers import make_session


class AgentSnapshotContractTests(TestCase):
    """契约型 / 元型断言。仅靠 import + 属性检查，保护未来重构不会悄悄改名。"""

    def test_documented_models_match_set(self):
        """如果有人改 ``_MODELS`` 但忘记 ``_DELETE_ORDER`` / ``_RESTORE_ORDER``，
        顺序不当会让 restore 在 FK 报错。强制要求两张表的覆盖顺序一致。
        """
        self.assertEqual(set(_MODELS), set(_DELETE_ORDER))
        self.assertEqual(set(_MODELS), set(_RESTORE_ORDER))

    def test_restore_order_matches_models(self):
        """``_RESTORE_ORDER`` 必须与 ``_MODELS`` 相同——
        重建顺序就是原 capture 顺序：先恢复父表、子表排在最后；否则 FK 会冲突。
        """
        self.assertEqual(_RESTORE_ORDER, _MODELS)

    def test_delete_order_puts_fk_children_before_parents(self):
        """``_DELETE_ORDER`` 的核心不变量：所有有 FK 指向其他表的（``_MODELS`` 中
        排在末尾的 Schedule / TaskDependency 等）必须排在删除序列前面。后两个
        独立表（``UserPreference`` / ``PlanVersion``）可以放在任何位置，但保持
        当前排序。
        """
        # 找出 MODELS 中"被引用"的部分（不带独立表）
        # 简单粗暴按位置：前 4 个是被引用方（Task/Event/TaskDependency/Schedule），
        # 后 2 个是独立表。验证：
        referenced = _MODELS[:4]
        independent = _MODELS[4:]

        # DELETE_ORDER 中 independent 必须排在 referenced 之后
        delete_index_of_independent = [_DELETE_ORDER.index(m) for m in independent]
        delete_index_of_referenced = [_DELETE_ORDER.index(m) for m in referenced]
        self.assertGreater(
            min(delete_index_of_independent),
            max(delete_index_of_referenced),
            "独立表应排在被引用表之后删除（FK 不阻塞）",
        )
        # 被引用表本身的删除索引必须严格递减（reverse of capture order）
        for earlier, later in zip(
            delete_index_of_referenced, delete_index_of_referenced[1:]
        ):
            self.assertGreater(
                earlier,
                later,
                "被引用表的删除顺序必须是 capture 顺序的反向",
            )

    def test_tool_commit_not_allowed_error_is_public(self):
        """``ToolCommitNotAllowedError`` 必须保留为 RuntimeError 子类——它在
        docstring / 注释里被引用，未来若有人删了类型，契约就丢了。
        """
        self.assertTrue(issubclass(ToolCommitNotAllowedError, RuntimeError))


class AgentSnapshotRoundTripTests(TestCase):
    """功能行为：capture → 修改 → restore 必须真的回到 capture 时刻。"""

    def setUp(self):
        self.db = make_session()

    def _make_task(self, title, duration=60):
        task = Task(
            title=title,
            estimated_duration=duration,
            priority="medium",
            status="pending",
        )
        self.db.add(task)
        self.db.commit()
        self.db.refresh(task)
        return task

    def test_capture_and_restore_round_trip_unchanged(self):
        """初始数据库 → 捕获 → 直接还原 → 行数与内容完全一致。"""
        self._make_task("write")
        snapshot = AgentDatabaseSnapshot.capture(self.db)
        snapshot.restore(self.db)
        # restore 后不能再用原引用，必须重新查询
        tasks = self.db.query(Task).all()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "write")

    def test_restore_rolls_back_partial_insert(self):
        """snapshot 后插入新行 + 修改旧行 → restore 后只剩原内容。"""
        self._make_task("first")
        snapshot = AgentDatabaseSnapshot.capture(self.db)

        # 中途新增
        self._make_task("second")
        # 中途修改 first 的 title
        first = self.db.query(Task).filter(Task.title == "first").one()
        first.title = "first-modified"
        self.db.commit()

        # 此时数据库确实有 2 条
        self.assertEqual(self.db.query(Task).count(), 2)

        snapshot.restore(self.db)

        # restore 后只剩 1 条，title 回到 "first"
        tasks = self.db.query(Task).all()
        self.assertEqual(len(tasks), 1, f"restore 后应只剩 1 行，实有 {len(tasks)}")
        self.assertEqual(tasks[0].title, "first")

    def test_restore_keeps_original_ids(self):
        """回滚后 id 应该与原始行一致，不应该重新分配。

        在 restore 之前把 id 记下来，restore 之后重新查询校验。
        """
        original = self._make_task("id-stable")
        original_id = original.id
        snapshot = AgentDatabaseSnapshot.capture(self.db)
        snapshot.restore(self.db)
        task = self.db.query(Task).one()
        self.assertEqual(task.id, original_id)

    def test_restore_preserves_all_columns(self):
        """回滚后所有原列值都还在，不丢字段。"""
        self._make_task("fields", duration=120)
        snapshot = AgentDatabaseSnapshot.capture(self.db)
        snapshot.restore(self.db)
        task = self.db.query(Task).one()
        self.assertEqual(task.title, "fields")
        self.assertEqual(task.estimated_duration, 120)
        self.assertEqual(task.priority, "medium")
        self.assertEqual(task.status, "pending")

    def test_holding_old_reference_raises_detached(self):
        """显式验证 ``db.expire_all()`` 的副作用：restore 之前持有的 ORM 引用
        再访问会触发 ``DetachedInstanceError``。这是有意为之的保护——上层不该
        跨 restore 持有引用。
        """
        original = self._make_task("cross-ref")
        snapshot = AgentDatabaseSnapshot.capture(self.db)
        snapshot.restore(self.db)
        with self.assertRaises(Exception) as ctx:
            # 这一步走 attribute refresh，触发 detached
            _ = original.title
        # SQLAlchemy 1.4/2.0 用 DetachedInstanceError；笼统地用 Exception 接
        self.assertIn("DetachedInstanceError", type(ctx.exception).__name__)
