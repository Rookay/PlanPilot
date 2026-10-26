# -*- coding: UTF-8 -*-
"""测试计划版本记录与撤销。

从原先 1412 行、33 个用例挤在一个 ServiceTests 类里拆出来：
失败时能直接看出是哪个域坏了，也可以单独跑一个文件。
"""

from datetime import datetime
from unittest import TestCase
from unittest.mock import patch
from app.schemas.schedule import ScheduleUpdate
from app.schemas.task import TaskCreate
from app.services import schedule_service, task_service
from tests.helpers import make_session


class PlanVersionTests(TestCase):
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


    def test_latest_plan_version_can_be_undone(self):
        task = task_service.create_task(self.db, TaskCreate(title="可撤销任务"))
        schedule = schedule_service.create_schedule(
            self.db,
            task.id,
            datetime(2026, 9, 16, 1),
            datetime(2026, 9, 16, 2),
            status="accepted",
        )
        schedule_service.update_schedule(
            self.db,
            schedule.id,
            ScheduleUpdate(
                start_time=datetime(2026, 9, 16, 3),
                end_time=datetime(2026, 9, 16, 4),
            ),
        )
        schedule_service.undo_latest_plan(self.db)
        restored = schedule_service.list_schedules(self.db)[0]
        self.assertEqual(restored.start_time, datetime(2026, 9, 16, 1))
        self.assertEqual(restored.end_time, datetime(2026, 9, 16, 2))

    def test_all_task_schedule_chunks_can_be_removed_and_undone(self):
        task = task_service.create_task(
            self.db,
            TaskCreate(title="分段论文", estimated_duration=120),
        )
        other = task_service.create_task(
            self.db,
            TaskCreate(title="其它任务", estimated_duration=60),
        )
        first = schedule_service.create_schedule(
            self.db, task.id, datetime(2026, 9, 18, 1), datetime(2026, 9, 18, 2)
        )
        second = schedule_service.create_schedule(
            self.db, task.id, datetime(2026, 9, 18, 3), datetime(2026, 9, 18, 4)
        )
        other_schedule = schedule_service.create_schedule(
            self.db, other.id, datetime(2026, 9, 18, 5), datetime(2026, 9, 18, 6)
        )

        deleted = schedule_service.delete_task_schedules(self.db, task.id)

        self.assertEqual(deleted["ids"], [first.id, second.id])
        self.assertIsNotNone(task_service.get_task(self.db, task.id))
        self.assertEqual([item.id for item in schedule_service.list_schedules(self.db)], [other_schedule.id])

        schedule_service.undo_latest_plan(self.db)
        restored_ids = {item.id for item in schedule_service.list_schedules(self.db)}
        self.assertEqual(restored_ids, {first.id, second.id, other_schedule.id})
