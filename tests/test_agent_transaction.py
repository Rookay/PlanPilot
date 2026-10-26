# -*- coding: UTF-8 -*-
"""``AgentDatabaseSnapshot`` 行为测试。

这里守住两件事：

1. **只撤销本次确认流的改动**。旧实现回滚时 ``DELETE`` 整表再灌回快照，
   并发下会抹掉别的会话刚提交的行，且不报错。现在用写集白名单决定删什么。
2. **service 层自己 commit 是常态**。``delete_event`` / ``delete_task`` 都在
   service 里提交，所以撤销点必须是"补偿"而不是事务回滚——哪怕已经提交过，
   也要能修回原状。
"""

import os
import tempfile
from pathlib import Path
from unittest import TestCase

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401  建表需要先导入所有模型
from app.agent.transaction import (
    AgentDatabaseSnapshot,
    ToolCommitNotAllowedError,
    _MODELS,
    _RESTORE_ORDER,
)
from app.database import Base, configure_sqlite_connection
from app.models.event import Event
from app.models.schedule import Schedule
from app.models.task import Task, TaskDependency
from tests.helpers import make_session


class SnapshotContractTests(TestCase):
    """契约型断言：靠静态扫描，不依赖运行时。"""

    def test_tool_commit_not_allowed_error_is_public(self):
        # 这个类型在 transaction.py 的注释里被引用，删掉契约就丢了。
        self.assertTrue(issubclass(ToolCommitNotAllowedError, RuntimeError))

    def test_restore_order_puts_parents_before_children(self):
        """外键要求父表先恢复，否则插入会被挡住。"""

        self.assertEqual(_RESTORE_ORDER, _MODELS)
        for dependency in (Schedule, TaskDependency):
            self.assertLess(
                _RESTORE_ORDER.index(Task),
                _RESTORE_ORDER.index(dependency),
                f"{dependency.__name__} 依赖 Task，必须排在后面",
            )
        self.assertLess(
            _RESTORE_ORDER.index(Event),
            _RESTORE_ORDER.index(TaskDependency),
        )

    def test_no_tool_opens_its_own_session(self):
        """工具新开 Session 会让写集抓不到，失败时无法补偿。"""

        tools_dir = Path(__file__).resolve().parent.parent / "app" / "tools"
        offenders = []
        for path in sorted(tools_dir.glob("*.py")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "SessionLocal(" in stripped or "Session(" in stripped:
                    offenders.append(f"{path.name}:{lineno}: {stripped}")
        self.assertEqual(
            offenders,
            [],
            "工具不得新开 Session：写集抓不到就无法回滚\n" + "\n".join(offenders),
        )


class SnapshotRoundTripTests(TestCase):
    """capture → 修改 → restore 必须回到 capture 时刻。"""

    def setUp(self):
        self.db = make_session()

    def tearDown(self):
        self.db.close()

    def _seed_task(self, title, duration=60):
        """capture 之前就存在的历史数据。"""

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

    def _add_task(self, title, commit=False):
        task = Task(
            title=title,
            estimated_duration=30,
            priority="low",
            status="pending",
        )
        self.db.add(task)
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return task

    def test_round_trip_leaves_data_unchanged(self):
        self._seed_task("write")
        snapshot = AgentDatabaseSnapshot.capture(self.db)

        snapshot.restore(self.db)

        tasks = self.db.query(Task).all()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "write")

    def test_restore_undoes_partial_inserts_and_updates(self):
        self._seed_task("first")
        snapshot = AgentDatabaseSnapshot.capture(self.db)

        self._add_task("second")
        first = self.db.query(Task).filter(Task.title == "first").one()
        first.title = "first-modified"
        self.db.flush()

        self.assertEqual(self.db.query(Task).count(), 2)

        snapshot.restore(self.db)

        tasks = self.db.query(Task).all()
        self.assertEqual(len(tasks), 1, f"restore 后应只剩 1 行，实有 {len(tasks)}")
        self.assertEqual(tasks[0].title, "first", "被改的字段也要退回去")

    def test_restore_undoes_changes_already_committed_by_service(self):
        """service 层自己 commit 是常态，撤销点必须仍然有效。

        这条是整个补偿机制的存在理由：``delete_event`` 在 service 里就
        ``db.commit()`` 了，事务回滚救不回来，只能靠写集补偿。
        """

        self._seed_task("baseline")
        snapshot = AgentDatabaseSnapshot.capture(self.db)

        self._add_task("already-committed", commit=True)

        snapshot.restore(self.db)

        titles = {task.title for task in self.db.query(Task).all()}
        self.assertNotIn("already-committed", titles, "已提交的新增也要能撤销")
        self.assertIn("baseline", titles)

    def test_restore_brings_back_deleted_rows(self):
        task = self._seed_task("will-be-deleted")
        original_id = task.id
        snapshot = AgentDatabaseSnapshot.capture(self.db)

        self.db.delete(task)
        self.db.commit()

        snapshot.restore(self.db)

        restored = self.db.query(Task).one()
        self.assertEqual(restored.id, original_id)
        self.assertEqual(restored.title, "will-be-deleted")

    def test_restore_keeps_original_ids(self):
        original = self._seed_task("id-stable")
        original_id = original.id

        snapshot = AgentDatabaseSnapshot.capture(self.db)
        self._add_task("bumps-the-sequence")
        snapshot.restore(self.db)

        task = self.db.query(Task).one()
        self.assertEqual(task.id, original_id)

    def test_reference_after_restore_sees_rolled_back_value(self):
        task = self._seed_task("before")
        snapshot = AgentDatabaseSnapshot.capture(self.db)
        task.title = "after"
        self.db.flush()

        snapshot.restore(self.db)

        self.assertEqual(task.title, "before")


class SnapshotIsolationTests(TestCase):
    """回滚不能碰别人的数据。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        path = os.path.join(self._tmpdir.name, "snapshot.db")
        self.engine = create_engine(
            f"sqlite:///{path}",
            connect_args={"check_same_thread": False, "timeout": 15},
            pool_size=5,
            max_overflow=5,
            future=True,
        )

        @event.listens_for(self.engine, "connect")
        def _pragma(connection, _record):
            configure_sqlite_connection(connection)

        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, future=True)

    def tearDown(self):
        self.engine.dispose()
        self._tmpdir.cleanup()

    def test_restore_keeps_rows_committed_by_another_session(self):
        """A 建立撤销点之后，B 提交了新数据；A 回滚时 B 的数据必须还在。

        旧实现会在此时执行 ``DELETE FROM tasks`` 再灌回快照，B 那行随之消失。
        """

        session_a = self.Session()
        session_b = self.Session()
        try:
            snapshot = AgentDatabaseSnapshot.capture(session_a)

            # B 在 A 的撤销点之外独立提交
            session_b.add(
                Task(title="B 的任务", estimated_duration=30, priority="low", status="pending")
            )
            session_b.commit()

            # A 也新增一条，随后失败
            session_a.add(
                Task(title="A 的任务", estimated_duration=30, priority="low", status="pending")
            )
            session_a.commit()

            snapshot.restore(session_a)

            titles = {task.title for task in session_a.query(Task).all()}
            self.assertIn("B 的任务", titles, "别人的提交不能被回滚抹掉")
            self.assertNotIn("A 的任务", titles, "自己的改动必须撤销")
        finally:
            session_a.close()
            session_b.close()

    def test_restore_does_not_delete_rows_added_after_capture_by_others(self):
        """连"别的会话在我回滚之前立刻插入"这种情况也要保住。"""

        session_a = self.Session()
        session_b = self.Session()
        try:
            snapshot = AgentDatabaseSnapshot.capture(session_a)
            for index in range(3):
                session_b.add(
                    Task(
                        title=f"B 的并发任务 {index}",
                        estimated_duration=30,
                        priority="low",
                        status="pending",
                    )
                )
            session_b.commit()

            snapshot.restore(session_a)

            titles = {task.title for task in session_a.query(Task).all()}
            self.assertEqual(
                titles,
                {f"B 的并发任务 {i}" for i in range(3)},
                "A 什么都没写，回滚不该动任何东西",
            )
        finally:
            session_a.close()
            session_b.close()

    def test_release_keeps_own_changes(self):
        """成功路径：只撤监听，改动保留。"""

        session = self.Session()
        try:
            snapshot = AgentDatabaseSnapshot.capture(session)
            session.add(
                Task(title="要保留", estimated_duration=30, priority="low", status="pending")
            )
            session.commit()

            snapshot.release(session)

            titles = {task.title for task in session.query(Task).all()}
            self.assertIn("要保留", titles)
        finally:
            session.close()
