# -*- coding: UTF-8 -*-
"""Memory 并发安全单元测试。

这些测试不依赖数据库、Agent 或 LLM，只验证 ``app.agent.memory.Memory``
在多线程下的正确性。锁加入前，并发 ``add`` + ``recent`` + ``replace_last_assistant``
会引发 ``RuntimeError`` 或读到不一致的列表快照。
"""

from threading import Barrier, Thread
from unittest import TestCase

from app.agent.memory import Memory, _HISTORY_MAX


class MemorySequentialBehaviourTests(TestCase):
    """顺序场景，确保加锁不破坏原有行为。"""

    def test_add_and_recent(self):
        memory = Memory()
        memory.add("s", "user", "hello")
        memory.add("s", "assistant", "hi")
        recent = memory.recent("s")
        self.assertEqual([m["role"] for m in recent], ["user", "assistant"])
        self.assertEqual(recent[0]["content"], "hello")
        self.assertEqual(recent[1]["content"], "hi")

    def test_history_capped_at_max(self):
        memory = Memory()
        for index in range(_HISTORY_MAX + 25):
            memory.add("s", "user", f"msg-{index}")
        history_length = memory.size("s")
        self.assertEqual(history_length, _HISTORY_MAX)
        # 最早条目已被裁剪，剩下的第一条应是能装下的最早那条
        recent = memory.recent("s", limit=_HISTORY_MAX)
        self.assertEqual(recent[0]["content"], f"msg-{25}")
        self.assertEqual(recent[-1]["content"], f"msg-{_HISTORY_MAX + 24}")

    def test_hydrate_only_when_missing(self):
        memory = Memory()
        memory.add("s", "user", "before")
        memory.hydrate("s", [{"role": "user", "content": "after"}])
        # hydrate 不应覆盖已有内存
        self.assertEqual(memory.recent("s")[0]["content"], "before")
        memory.clear("s")
        memory.hydrate("s", [{"role": "user", "content": "fresh"}])
        self.assertEqual(memory.recent("s")[0]["content"], "fresh")

    def test_replace_last_assistant_only_changes_match(self):
        memory = Memory()
        memory.add("s", "user", "u")
        memory.add("s", "assistant", "a")
        memory.add("s", "user", "u2")
        memory.replace_last_assistant("s", "a-after")
        messages = memory.recent("s")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-2]["content"], "a-after")

    def test_replace_last_assistant_no_match_is_noop(self):
        memory = Memory()
        memory.add("s", "user", "u")
        memory.replace_last_assistant("s", "ignored")
        self.assertEqual(memory.recent("s")[0]["content"], "u")


class MemoryConcurrencyTests(TestCase):
    """并发场景，验证 ``threading.Lock`` 已经把对 ``_sessions`` 的读写串行化。"""

    @staticmethod
    def _race_workers(memory, session_id, total_ops, barrier):
        """每个线程在 barrier 等齐后开始，并发交错做 add / recent / replace。"""

        results = {"errors": []}

        def worker(worker_id):
            try:
                barrier.wait(timeout=5)
                for index in range(total_ops):
                    memory.add(session_id, "user", f"w{worker_id}-{index}")
                    memory.add(session_id, "assistant", f"a{worker_id}-{index}")
                    _ = memory.recent(session_id, limit=5)
                    if index % 4 == 0:
                        memory.replace_last_assistant(
                            session_id, f"final-w{worker_id}-{index}"
                        )
            except Exception as exc:  # noqa: BLE001
                results["errors"].append(exc)

        return worker, results

    def test_concurrent_add_read_replace_does_not_raise(self):
        memory = Memory()
        thread_count = 16
        ops_per_thread = 40
        barrier = Barrier(thread_count)
        worker, results = self._race_workers(memory, "race", ops_per_thread, barrier)

        threads = [Thread(target=worker, args=(idx,)) for idx in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(
            results["errors"],
            [],
            f"并发操作不应抛异常，实际错误：{results['errors'][:3]}...",
        )

    def test_concurrent_add_keeps_history_under_cap(self):
        memory = Memory()
        thread_count = 16
        ops_per_thread = 20  # 总写入 = 16 * 40 = 640，远超 _HISTORY_MAX
        barrier = Barrier(thread_count)
        worker, _ = self._race_workers(memory, "cap", ops_per_thread, barrier)

        threads = [Thread(target=worker, args=(idx,)) for idx in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        # 由于加锁，只有恰好 _HISTORY_MAX 条；没有锁时 ``del history[:-_HISTORY_MAX]``
        # 与并发的 append 可能把长度顶过上限甚至挂掉。
        self.assertLessEqual(memory.size("cap"), _HISTORY_MAX)
        self.assertGreater(memory.size("cap"), 0)

    def test_concurrent_clear_and_hydrate_safety(self):
        """一个线程 clear + hydrate，另一个线程狂 add；不该出现 RuntimeError。"""

        memory = Memory()
        thread_count = 8
        barriers = {"start": Barrier(thread_count)}
        errors: list = []

        def adder(worker_id):
            try:
                barriers["start"].wait(timeout=5)
                for index in range(50):
                    memory.add("shared", "user", f"add-{worker_id}-{index}")
                    _ = memory.recent("shared")
            except Exception as exc:  # noqa: BLE001
                errors.append(("adder", exc))

        def clearer():
            try:
                barriers["start"].wait(timeout=5)
                for _ in range(50):
                    memory.clear("shared")
                    memory.hydrate(
                        "shared",
                        [{"role": "user", "content": "hydrated"}],
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(("clearer", exc))

        threads = [Thread(target=adder, args=(idx,)) for idx in range(thread_count - 1)]
        threads.append(Thread(target=clearer))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [], f"并发 clear/hydrate 不应抛异常：{errors[:3]}")
