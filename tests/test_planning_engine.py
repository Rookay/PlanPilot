from datetime import datetime, timedelta
from unittest import TestCase

from app.planning.engine import PlanTask, generate_plan
from app.planning.optimizer import generate_optimized_plan, is_cp_sat_available


class PlanningEngineTests(TestCase):
    def test_deadline_first_does_not_let_distant_low_priority_task_displace_medium(self):
        if not is_cp_sat_available():
            self.skipTest("OR-Tools is not installed")
        start = datetime(2026, 9, 19, 9)
        tasks = [
            PlanTask(id=1, title="普通任务", priority="medium", estimated_duration=60),
            PlanTask(
                id=2,
                title="远期低优先级任务",
                priority="low",
                estimated_duration=60,
                deadline=start + timedelta(days=8),
            ),
        ]

        quick = generate_plan(
            start=start,
            days=1,
            tasks=tasks,
            busy_intervals=[],
            scheduled_task_ids=set(),
            windows=[(start, start + timedelta(hours=1))],
            strategy="deadline_first",
        )
        optimized = generate_optimized_plan(
            start=start,
            tasks=tasks,
            busy_intervals=[],
            scheduled_task_ids=set(),
            completed_task_ids=set(),
            dependency_completion_times={},
            windows=[(start, start + timedelta(hours=1))],
            gap_minutes=0,
            strategy="deadline_first",
        )

        self.assertEqual(quick.assignments[0].task_id, 1)
        self.assertEqual(optimized.assignments[0].task_id, 1)

    def test_deadline_first_protects_genuinely_urgent_task(self):
        if not is_cp_sat_available():
            self.skipTest("OR-Tools is not installed")
        start = datetime(2026, 9, 19, 9)
        tasks = [
            PlanTask(id=1, title="普通任务", priority="medium", estimated_duration=60),
            PlanTask(
                id=2,
                title="明日截止任务",
                priority="low",
                estimated_duration=60,
                deadline=start + timedelta(days=1),
            ),
        ]

        quick = generate_plan(
            start=start,
            days=1,
            tasks=tasks,
            busy_intervals=[],
            scheduled_task_ids=set(),
            windows=[(start, start + timedelta(hours=1))],
            strategy="deadline_first",
        )
        optimized = generate_optimized_plan(
            start=start,
            tasks=tasks,
            busy_intervals=[],
            scheduled_task_ids=set(),
            completed_task_ids=set(),
            dependency_completion_times={},
            windows=[(start, start + timedelta(hours=1))],
            gap_minutes=0,
            strategy="deadline_first",
        )

        self.assertEqual(quick.assignments[0].task_id, 2)
        self.assertEqual(optimized.assignments[0].task_id, 2)

    def test_cp_sat_finds_global_order_that_greedy_misses(self):
        if not is_cp_sat_available():
            self.skipTest("OR-Tools is not installed")
        start = datetime(2026, 9, 21, 9)
        tasks = [
            PlanTask(id=1, title="长任务", estimated_duration=120, priority="high"),
            PlanTask(
                id=2,
                title="临近截止任务",
                estimated_duration=60,
                priority="medium",
                deadline=datetime(2026, 9, 21, 10),
            ),
        ]
        greedy = generate_plan(
            start=start,
            days=1,
            tasks=tasks,
            busy_intervals=[],
            scheduled_task_ids=set(),
            windows=[(start, datetime(2026, 9, 21, 12))],
        )
        optimized = generate_optimized_plan(
            start=start,
            tasks=tasks,
            busy_intervals=[],
            scheduled_task_ids=set(),
            completed_task_ids=set(),
            dependency_completion_times={},
            windows=[(start, datetime(2026, 9, 21, 12))],
            gap_minutes=0,
            strategy="balanced",
        )

        self.assertEqual(len(greedy.assignments), 1)
        self.assertEqual(len(optimized.assignments), 2)
        self.assertEqual(optimized.assignments[0].task_id, 2)
    def test_priority_gap_and_preferred_window(self):
        start = datetime(2026, 9, 16, 1, 0)
        tasks = [
            PlanTask(id=1, title="普通任务", priority="medium", estimated_duration=60),
            PlanTask(
                id=2,
                title="论文",
                priority="high",
                estimated_duration=60,
                preferred_windows=[
                    (datetime(2026, 9, 16, 5, 0), datetime(2026, 9, 16, 9, 0))
                ],
            ),
        ]

        result = generate_plan(
            start=start,
            days=1,
            tasks=tasks,
            busy_intervals=[],
            scheduled_task_ids=set(),
            windows=[(start, datetime(2026, 9, 16, 10, 0))],
            gap_minutes=30,
            strategy="priority_first",
        )

        self.assertEqual([item.task_id for item in result.assignments], [2, 1])
        self.assertEqual(result.assignments[0].start_time, datetime(2026, 9, 16, 5, 0))
        first, second = sorted(result.assignments, key=lambda item: item.start_time)
        self.assertGreaterEqual(second.start_time, first.end_time + timedelta(minutes=30))
        self.assertIn("符合偏好时段", result.assignments[0].reason)

    def test_deadline_failure_has_readable_reason(self):
        start = datetime(2026, 9, 16, 1, 0)
        result = generate_plan(
            start=start,
            days=1,
            tasks=[PlanTask(id=1, title="过期任务", deadline=start - timedelta(minutes=1))],
            busy_intervals=[],
            scheduled_task_ids=set(),
            windows=[(start, start + timedelta(hours=4))],
        )
        self.assertEqual(len(result.failures), 1)
        self.assertIn("截止时间", result.failures[0].reason)

    def test_splittable_task_creates_multiple_chunks(self):
        start = datetime(2026, 9, 16, 1)
        result = generate_plan(
            start=start,
            days=1,
            tasks=[
                PlanTask(
                    id=1,
                    title="长任务",
                    estimated_duration=180,
                    splittable=True,
                    min_chunk_minutes=30,
                    max_chunk_minutes=60,
                )
            ],
            busy_intervals=[],
            scheduled_task_ids=set(),
            windows=[(start, start + timedelta(hours=5))],
        )
        self.assertEqual(len(result.assignments), 3)
        self.assertEqual(
            [item.chunk_index for item in result.assignments],
            [1, 2, 3],
        )
        self.assertEqual(
            sum(
                int((item.end_time - item.start_time).total_seconds() // 60)
                for item in result.assignments
            ),
            180,
        )

    def test_dependencies_are_scheduled_in_order(self):
        start = datetime(2026, 9, 16, 1)
        result = generate_plan(
            start=start,
            days=1,
            tasks=[
                PlanTask(id=2, title="写报告", priority="high", dependency_ids=[1]),
                PlanTask(id=1, title="做实验", priority="low"),
            ],
            busy_intervals=[],
            scheduled_task_ids=set(),
            windows=[(start, start + timedelta(hours=4))],
        )
        first, second = result.assignments
        self.assertEqual([first.task_id, second.task_id], [1, 2])
        self.assertGreaterEqual(second.start_time, first.end_time)
