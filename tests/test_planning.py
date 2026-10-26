# -*- coding: UTF-8 -*-
"""测试计划生成、规划模式选择与局部重排。

从原先 1412 行、33 个用例挤在一个 ServiceTests 类里拆出来：
失败时能直接看出是哪个域坏了，也可以单独跑一个文件。
"""

from datetime import datetime
from unittest import TestCase
from unittest.mock import patch
from app.models.schedule import Schedule
from fastapi import HTTPException
from app.schemas.event import EventCreate
from app.schemas.preference import PreferenceUpdate
from app.schemas.schedule import (
    LocalReplanRequest,
    ScheduleGenerateRequest,
    ScheduleUpdate,
)
from app.schemas.task import TaskCreate
from app.services import event_service, preference_service, schedule_service, task_service
from tests.helpers import make_session


class PlanningServiceTests(TestCase):
    def setUp(self):
        self.time_patcher = patch(
            "app.services.time_policy.utcnow",
            return_value=datetime(2026, 9, 15),
        )
        self.time_patcher.start()
        self.db = make_session()

    def tearDown(self):
        self.db.close()
        self.time_patcher.stop()


    def test_new_items_cannot_be_scheduled_in_the_past(self):
        with patch(
            "app.services.time_policy.utcnow",
            return_value=datetime(2026, 9, 18),
        ):
            with self.assertRaises(HTTPException) as event_error:
                event_service.create_event(
                    self.db,
                    EventCreate(
                        title="过去的会议",
                        start_time=datetime(2026, 9, 17, 2),
                        end_time=datetime(2026, 9, 17, 3),
                    ),
                )
            with self.assertRaises(HTTPException) as task_error:
                task_service.create_task(
                    self.db,
                    TaskCreate(
                        title="过去的任务",
                        start_time=datetime(2026, 9, 17, 2),
                    ),
                )

            task = task_service.create_task(
                self.db,
                TaskCreate(title="未来任务", estimated_duration=60),
            )
            with self.assertRaises(HTTPException) as schedule_error:
                schedule_service.create_schedule(
                    self.db,
                    task.id,
                    datetime(2026, 9, 17, 2),
                    datetime(2026, 9, 17, 3),
                )

            generated = schedule_service.generate_schedules(
                self.db,
                ScheduleGenerateRequest(
                    start=datetime(2026, 9, 16),
                    days=3,
                ),
            )

        self.assertIn("不能早于当前时间", str(event_error.exception.detail))
        self.assertIn("不能早于当前时间", str(task_error.exception.detail))
        self.assertIn("不能早于当前时间", str(schedule_error.exception.detail))
        self.assertTrue(generated.schedules)
        self.assertTrue(
            all(item.start_time >= datetime(2026, 9, 18) for item in generated.schedules)
        )

    def test_plan_preview_compares_changes_and_generate_keeps_other_ranges(self):
        inside = task_service.create_task(
            self.db,
            TaskCreate(title="本周任务", estimated_duration=60),
        )
        outside = task_service.create_task(
            self.db,
            TaskCreate(
                title="下周任务",
                estimated_duration=60,
                earliest_start=datetime(2026, 9, 25, 1),
            ),
        )
        outside_schedule = schedule_service.create_schedule(
            self.db,
            outside.id,
            datetime(2026, 9, 25, 1),
            datetime(2026, 9, 25, 2),
        )
        payload = ScheduleGenerateRequest(
            start=datetime(2026, 9, 16),
            days=1,
        )

        preview = schedule_service.preview_plan(self.db, payload)
        generated = schedule_service.generate_schedules(self.db, payload)

        self.assertIn("comparison", preview)
        self.assertGreaterEqual(preview["comparison"]["counts"]["added"], 1)
        self.assertTrue(
            any(item["task_id"] == inside.id for item in preview["comparison"]["changes"])
        )
        self.assertTrue(generated.schedules)
        self.assertIsNotNone(
            self.db.query(Schedule).filter(Schedule.id == outside_schedule.id).first()
        )

    def test_planning_mode_is_selected_and_explained(self):
        task_service.create_task(
            self.db,
            TaskCreate(title="简单任务", estimated_duration=60),
        )
        quick = schedule_service.preview_plan(
            self.db,
            ScheduleGenerateRequest(
                start=datetime(2026, 9, 16),
                days=1,
                planning_mode="quick",
            ),
        )
        self.assertEqual(quick["analysis"]["requested_mode"], "quick")
        self.assertEqual(quick["analysis"]["selected_mode"], "quick")
        self.assertIn("快速安排", quick["analysis"]["selection_reason"])

        optimized = schedule_service.preview_plan(
            self.db,
            ScheduleGenerateRequest(
                start=datetime(2026, 9, 16),
                days=1,
                planning_mode="optimized",
            ),
        )
        self.assertEqual(optimized["analysis"]["requested_mode"], "optimized")
        if optimized["analysis"]["selected_mode"] == "quick":
            self.assertTrue(optimized["analysis"]["fallback_reason"])
        else:
            self.assertEqual(optimized["analysis"]["selected_mode"], "optimized")

    def test_auto_mode_uses_complexity_signals(self):
        for index in range(6):
            task_service.create_task(
                self.db,
                TaskCreate(title=f"任务 {index + 1}", estimated_duration=30),
            )
        preview = schedule_service.preview_plan(
            self.db,
            ScheduleGenerateRequest(
                start=datetime(2026, 9, 16),
                days=2,
                planning_mode="auto",
            ),
        )
        analysis = preview["analysis"]
        self.assertEqual(analysis["requested_mode"], "auto")
        self.assertIn(analysis["selected_mode"], ("quick", "optimized"))
        if analysis["selected_mode"] == "optimized":
            self.assertIn("6 项待安排任务", analysis["selection_reason"])
        else:
            self.assertTrue(analysis["fallback_reason"])

    def test_plan_alternatives_are_preview_only_and_can_be_applied(self):
        for index in range(3):
            task_service.create_task(
                self.db,
                TaskCreate(
                    title=f"待规划任务 {index + 1}",
                    estimated_duration=60,
                    priority="high" if index == 0 else "medium",
                    deadline=datetime(2026, 9, 17, 10 + index),
                ),
            )
        payload = ScheduleGenerateRequest(
            start=datetime(2026, 9, 16, 1),
            days=2,
            gap_minutes=15,
        )

        alternatives = schedule_service.generate_plan_alternatives(self.db, payload)

        self.assertEqual(
            [item.profile for item in alternatives.alternatives],
            ["deadline", "balanced", "focus"],
        )
        self.assertIn(
            alternatives.recommended_profile,
            {"deadline", "balanced", "focus"},
        )
        self.assertEqual(schedule_service.list_schedules(self.db), [])
        self.assertTrue(all(item.schedules for item in alternatives.alternatives))
        self.assertTrue(all(item.metrics.scheduled_tasks == 3 for item in alternatives.alternatives))
        recommended = next(item for item in alternatives.alternatives if item.recommended)
        self.assertTrue(recommended.recommendation_reason)
        self.assertTrue(all(item.selection_policy for item in alternatives.alternatives))
        self.assertTrue(all(item.pace for item in alternatives.alternatives))
        self.assertTrue(all(item.outcome_labels for item in alternatives.alternatives))
        self.assertTrue(all(item.strengths for item in alternatives.alternatives))
        self.assertTrue(all(item.tradeoffs for item in alternatives.alternatives))
        self.assertEqual(
            sum(item.recommended for item in alternatives.alternatives),
            1,
        )
        by_profile = {item.profile: item for item in alternatives.alternatives}
        self.assertLessEqual(
            by_profile["focus"].metrics.idle_minutes,
            by_profile["balanced"].metrics.idle_minutes,
        )

        applied = schedule_service.apply_plan_alternative(
            self.db,
            payload,
            "focus",
        )

        self.assertEqual(len(applied.schedules), 3)
        self.assertTrue(applied.version_id)
        self.assertEqual(len(schedule_service.list_schedules(self.db)), 3)

    def test_local_preferences_drive_utc_planning_and_lunch_break(self):
        preference_service.update_preferences(
            self.db,
            PreferenceUpdate(
                timezone="Asia/Shanghai",
                workday_start="09:00",
                workday_end="18:00",
                lunch_start="12:00",
                lunch_end="13:00",
                default_gap_minutes=15,
            ),
        )
        task_service.create_task(
            self.db,
            TaskCreate(title="论文", priority="high", estimated_duration=120, preferred_time="下午"),
        )

        result = schedule_service.generate_schedules(
            self.db,
            ScheduleGenerateRequest(start=datetime(2026, 9, 16, 0, 0), days=1),
        )

        self.assertEqual(len(result.schedules), 1)
        # 上海下午 13:00 对应 UTC 05:00。
        self.assertEqual(result.schedules[0].start_time, datetime(2026, 9, 16, 5, 0))
        self.assertEqual(result.analysis.score, 100)
        self.assertTrue(result.batch_id)

        accepted = schedule_service.accept_all_schedules(self.db, result.batch_id)
        self.assertEqual(accepted["accepted"], 1)
        self.assertEqual(self.db.query(Schedule).one().status, "accepted")

    def test_locked_schedule_is_preserved_by_local_replan(self):
        first = task_service.create_task(
            self.db,
            TaskCreate(title="锁定任务", estimated_duration=60),
        )
        second = task_service.create_task(
            self.db,
            TaskCreate(title="可移动任务", estimated_duration=60),
        )
        locked = schedule_service.create_schedule(
            self.db,
            first.id,
            datetime(2026, 9, 16, 1),
            datetime(2026, 9, 16, 2),
            status="accepted",
        )
        schedule_service.update_schedule(
            self.db,
            locked.id,
            ScheduleUpdate(is_locked=True),
        )
        movable = schedule_service.create_schedule(
            self.db,
            second.id,
            datetime(2026, 9, 16, 4),
            datetime(2026, 9, 16, 5),
            status="accepted",
        )

        result = schedule_service.apply_local_replan(
            self.db,
            LocalReplanRequest(
                start=datetime(2026, 9, 16, 1),
                end=datetime(2026, 9, 16, 6),
            ),
        )

        schedules = schedule_service.list_schedules(self.db)
        kept = next(item for item in schedules if item.task_id == first.id)
        moved = next(item for item in schedules if item.task_id == second.id)
        self.assertEqual(kept.id, locked.id)
        self.assertTrue(kept.is_locked)
        self.assertEqual(moved.id, movable.id)
        self.assertEqual(moved.start_time, datetime(2026, 9, 16, 2))
        self.assertTrue(result["version_id"])
