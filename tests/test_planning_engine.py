from datetime import datetime, timedelta
from unittest import TestCase

from app.planning.engine import PlanTask, generate_plan


class PlanningEngineTests(TestCase):
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
