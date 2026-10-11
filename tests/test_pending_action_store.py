from unittest import TestCase

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agent.confirmation import PendingActionStore
from app.database import Base
from app import models  # noqa: F401
from app.models.pending_action import PendingAction
from tests.helpers import make_session


def _shared_session_factory():
    """返回一个 SessionLocal,多个 Session 共享同一份内存 SQLite 数据库。

    用来模拟"新请求拿到的新 Session 看到之前提交的写入"。
    """

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def _sample_tool_calls():
    return [
        {"name": "delete_task", "args": {"task_id": 1}},
        {"name": "delete_task", "args": {"task_id": 2}},
    ]


def _sample_preview():
    return {"items": [{"task_id": 1, "title": "A"}, {"task_id": 2, "title": "B"}]}


class PendingActionStoreTests(TestCase):
    def setUp(self):
        self.db = make_session()
        self.store = PendingActionStore()

    def tearDown(self):
        self.db.close()

    def test_create_returns_12_char_id(self):
        action_id = self.store.create(
            self.db,
            tool_calls=_sample_tool_calls(),
            summary="删除 2 个任务",
            preview=_sample_preview(),
            session_id="s1",
        )
        self.assertEqual(len(action_id), 12)
        self.assertTrue(all(c in "0123456789abcdef" for c in action_id))

    def test_create_persists_record(self):
        SessionLocal = _shared_session_factory()
        writer = SessionLocal()
        try:
            action_id = self.store.create(
                writer,
                tool_calls=_sample_tool_calls(),
                summary="删除 2 个任务",
                preview=_sample_preview(),
                session_id="s1",
            )
        finally:
            writer.close()

        # 重新开一个 session,模拟跨请求 / 跨进程读取。
        reader = SessionLocal()
        try:
            record = reader.get(PendingAction, action_id)
            self.assertIsNotNone(record)
            self.assertEqual(record.session_id, "s1")
            self.assertEqual(record.summary, "删除 2 个任务")
        finally:
            reader.close()

    def test_get_returns_view_with_tool_calls_and_preview(self):
        action_id = self.store.create(
            self.db,
            tool_calls=_sample_tool_calls(),
            summary="删除 2 个任务",
            preview=_sample_preview(),
            session_id="s1",
        )
        view = self.store.get(self.db, action_id, session_id="s1")
        self.assertIsNotNone(view)
        self.assertEqual(view.action_id, action_id)
        self.assertEqual(view.session_id, "s1")
        self.assertEqual(view.tool_calls, _sample_tool_calls())
        self.assertEqual(view.preview, _sample_preview())
        self.assertGreater(view.created_at, 0)

    def test_get_returns_none_for_missing(self):
        self.assertIsNone(self.store.get(self.db, "deadbeef", session_id="s1"))

    def test_get_isolates_sessions(self):
        action_id = self.store.create(
            self.db,
            tool_calls=_sample_tool_calls(),
            summary="x",
            preview=None,
            session_id="owner",
        )
        # 另一会话拿不到。
        self.assertIsNone(self.store.get(self.db, action_id, session_id="other"))
        # 不传 session_id 时跳过隔离校验(兼容旧的调用方)。
        self.assertIsNotNone(self.store.get(self.db, action_id))

    def test_remove_deletes_record(self):
        action_id = self.store.create(
            self.db,
            tool_calls=_sample_tool_calls(),
            summary="x",
            preview=None,
            session_id="s1",
        )
        self.store.remove(self.db, action_id)
        self.assertIsNone(self.db.get(PendingAction, action_id))

    def test_remove_is_idempotent(self):
        action_id = self.store.create(
            self.db,
            tool_calls=[],
            summary="x",
            preview=None,
            session_id="s1",
        )
        self.store.remove(self.db, action_id)
        # 第二次删除不应抛错。
        self.store.remove(self.db, action_id)

    def test_get_returns_none_after_ttl(self):
        action_id = self.store.create(
            self.db,
            tool_calls=_sample_tool_calls(),
            summary="x",
            preview=None,
            session_id="s1",
        )
        # 把 ttl 设成 0 秒,下一次 get 时立刻视为过期。
        self.store.ttl_seconds = 0
        self.assertIsNone(self.store.get(self.db, action_id, session_id="s1"))
        # 过期记录应被自动清理。
        self.assertIsNone(self.db.get(PendingAction, action_id))

    def test_get_handles_corrupted_json(self):
        action_id = self.store.create(
            self.db,
            tool_calls=_sample_tool_calls(),
            summary="x",
            preview=_sample_preview(),
            session_id="s1",
        )
        record = self.db.get(PendingAction, action_id)
        record.tool_calls_json = "{not-json"
        record.preview_json = "[oops"
        self.db.commit()

        view = self.store.get(self.db, action_id, session_id="s1")
        self.assertIsNotNone(view)
        self.assertEqual(view.tool_calls, [])
        self.assertIsNone(view.preview)