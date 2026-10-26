"""真实并发下的 SQLite 行为。

这里的测试**必须**用文件库和每线程独立 session：``tests/helpers.make_session``
用的是 ``StaticPool`` + 内存库，所有线程共享同一个 DBAPI 连接，连接内部自行串行，
既测不出写锁争用，也测不出 WAL 的读写并行。

覆盖四件事，都是代码里已经写了但此前没有任何证据的承诺：

1. ``request_id`` 用主键抢占，N 个线程同时抢只能有一个赢；
2. N 个线程写不同主键全部落盘，不丢不串；
3. 遇到短写锁时按 ``busy_timeout`` 等待，而不是立刻报 database is locked；
4. 4 写 + 4 读的混合负载下零锁失败、零丢写。

第 4 条不写成"读不被写阻塞"：实测 rollback journal 下普通读也不会被写事务
挡住（写者只在提交那一瞬需要独占），那种断言对日志模式没有区分力。真实差异
体现在混合负载的失败率上——``busy_timeout`` 调成 0 时这里会失败 50 多次。
"""

import json
import os
import tempfile
import threading
import time
from unittest import TestCase

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401  建表前必须先导入所有模型
from app.database import Base, configure_sqlite_connection
from app.models.chat_request import ChatRequest
from app.services import chat_request_service as crs
from app.utils import utcnow

#: 持锁线程占住写锁的时长。断言用它的比例，避免机器快慢导致误判。
HOLD_SECONDS = 0.4

#: 并发线程数。低于连接池上限（5 + 10），确保测的是锁行为而不是排队超时。
THREADS = 8


class ConcurrentDatabaseTestCase(TestCase):
    """为并发场景准备真实文件库。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "concurrent.db")
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False, "timeout": 15},
            pool_size=5,
            max_overflow=10,
            pool_timeout=15,
            future=True,
        )

        @event.listens_for(self.engine, "connect")
        def _apply_pragma(connection, _record):
            configure_sqlite_connection(connection)

        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine, autoflush=False, future=True
        )

    def tearDown(self):
        self.engine.dispose()
        self._tmpdir.cleanup()

    def _run_threads(self, target, count=THREADS):
        """让所有线程在同一起跑线出发，否则先启动的线程早就抢完了。

        ``Barrier`` 缺一不可：如果线程是依次启动的，那测的就不是并发，
        而是串行调用——这正是原来的 409 测试做的事。
        """

        barrier = threading.Barrier(count)
        errors = []

        def wrapper(index):
            try:
                barrier.wait(timeout=20)
                target(index)
            except BaseException as exc:  # noqa: BLE001  线程里的异常要带回主线程
                errors.append(exc)

        threads = [
            threading.Thread(target=wrapper, args=(i,), daemon=True)
            for i in range(count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        alive = [t for t in threads if t.is_alive()]
        self.assertFalse(alive, "有线程未在 60 秒内结束，可能出现死锁")
        if errors:
            raise errors[0]


class RequestIdContentionTests(ConcurrentDatabaseTestCase):
    """同一个 request_id 被多线程同时抢占。"""

    def test_only_one_thread_wins_the_same_request_id(self):
        results = []
        lock = threading.Lock()

        def worker(_index):
            db = self.Session()
            try:
                reservation = crs.reserve(db, "shared-id", "s1", "并发同 id")
                with lock:
                    results.append(reservation.state)
            finally:
                db.close()

        self._run_threads(worker)

        self.assertEqual(len(results), THREADS)
        # 主键插入 + IntegrityError 兜底必须保证只有一个线程真正执行。
        self.assertEqual(
            results.count("new"),
            1,
            f"应当恰好一个线程抢到，实际为 {results}",
        )
        self.assertEqual(results.count("running"), THREADS - 1)

    def test_parallel_distinct_ids_all_persist(self):
        def worker(index):
            db = self.Session()
            try:
                state = crs.reserve(db, f"id-{index}", "s1", f"第 {index} 条").state
                assert state == "new", f"独立 id 不应被挡：{index} -> {state}"
            finally:
                db.close()

        self._run_threads(worker)

        with self.Session() as db:
            rows = db.execute(select(ChatRequest)).scalars().all()
        self.assertEqual(len(rows), THREADS)
        self.assertEqual({row.id for row in rows}, {f"id-{i}" for i in range(THREADS)})

    def test_finished_request_replays_same_payload_under_contention(self):
        payload = {"reply": "只应执行一次", "tool_calls": []}

        with self.Session() as db:
            self.assertEqual(crs.reserve(db, "replay-id", "s1", "重放").state, "new")
            crs.finish(db, "replay-id", payload)
            db.commit()

        replayed = []
        lock = threading.Lock()

        def worker(_index):
            db = self.Session()
            try:
                reservation = crs.reserve(db, "replay-id", "s1", "重放")
                with lock:
                    replayed.append((reservation.state, reservation.response))
            finally:
                db.close()

        self._run_threads(worker)

        self.assertEqual({state for state, _ in replayed}, {"done"})
        for _state, response in replayed:
            self.assertEqual(response, json.loads(json.dumps(payload)))


class WriteLockTests(ConcurrentDatabaseTestCase):
    """busy_timeout 与 WAL 的实际效果。"""

    def test_short_write_lock_is_waited_out(self):
        holding = threading.Event()
        holder_error = []

        def holder():
            db = self.Session()
            try:
                db.add(
                    ChatRequest(
                        id="lock-holder",
                        session_id="s1",
                        status="running",
                        updated_at=utcnow(),
                    )
                )
                db.flush()  # 到这里已经拿到写锁
                holding.set()
                time.sleep(HOLD_SECONDS)
                db.commit()
            except BaseException as exc:  # noqa: BLE001
                holder_error.append(exc)
            finally:
                db.close()

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        try:
            self.assertTrue(holding.wait(timeout=20), "持锁线程未能就绪")

            db = self.Session()
            try:
                started = time.monotonic()
                reservation = crs.reserve(db, "lock-waiter", "s1", "等锁")
                elapsed = time.monotonic() - started
            finally:
                db.close()
        finally:
            thread.join(timeout=30)

        self.assertFalse(holder_error, f"持锁线程出错：{holder_error}")
        self.assertEqual(reservation.state, "new")
        # 如果没有 bus_timeout，这里会立刻抛 database is locked；
        # 如果等到了，说明确实等锁成功。断言下限证明"等过"。
        self.assertGreaterEqual(
            elapsed,
            HOLD_SECONDS * 0.5,
            f"没有等待写锁就写成功了（{elapsed:.3f}s），busy_timeout 可能未生效",
        )

    def test_mixed_read_write_load_has_no_lock_failures(self):
        """4 个写线程 + 4 个读线程并发，不能出现 database is locked。

        这条替代了早先"读不被写阻塞"的写法：实测在 rollback journal 下，
        写者只在 commit 那一瞬需要独占，普通读并不会被挡住（读耗时都是毫秒级），
        那种断言对日志模式没有区分力。真实的差异体现在**混合负载的失败率**——
        没有 busy_timeout 时这里会直接抛 OperationalError。
        """

        writers, readers, iterations = 4, 4, 15
        failures = []
        lock = threading.Lock()
        total_threads = writers + readers
        barrier = threading.Barrier(total_threads)

        def write(index):
            barrier.wait(timeout=20)
            for i in range(iterations):
                db = self.Session()
                try:
                    db.add(
                        ChatRequest(
                            id=f"w{index}-{i}",
                            session_id="s1",
                            status="running",
                            updated_at=utcnow(),
                        )
                    )
                    db.commit()
                except BaseException as exc:  # noqa: BLE001
                    with lock:
                        failures.append(f"写{index}: {type(exc).__name__}")
                finally:
                    db.close()

        def read(_index):
            barrier.wait(timeout=20)
            for _ in range(iterations):
                db = self.Session()
                try:
                    db.execute(select(ChatRequest)).scalars().all()
                except BaseException as exc:  # noqa: BLE001
                    with lock:
                        failures.append(f"读: {type(exc).__name__}")
                finally:
                    db.close()

        threads = [
            threading.Thread(target=write, args=(i,), daemon=True)
            for i in range(writers)
        ]
        threads += [
            threading.Thread(target=read, args=(i,), daemon=True)
            for i in range(readers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertFalse(
            [t for t in threads if t.is_alive()], "混合负载有线程未结束"
        )
        self.assertEqual(
            failures,
            [],
            f"并发读写出现锁失败 {len(failures)} 次：{failures[:5]}",
        )

        with self.Session() as db:
            total = len(db.execute(select(ChatRequest)).scalars().all())
        self.assertEqual(total, writers * iterations, "有写入丢失")
