# -*- coding: UTF-8 -*-
"""测试日程增删改与冲突提示。

从原先 1412 行、33 个用例挤在一个 ServiceTests 类里拆出来：
失败时能直接看出是哪个域坏了，也可以单独跑一个文件。
"""

from datetime import datetime
from unittest import TestCase
from unittest.mock import patch
from app.models.schedule import Schedule
from fastapi import HTTPException
from app.schemas.event import EventSeriesCreate
from app.schemas.schedule import ScheduleUpdate
from app.schemas.task import TaskCreate, TaskUpdate
from app.services import event_service, schedule_service, task_service
from tests.helpers import make_session


class ScheduleCrudTests(TestCase):
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


    def test_manual_schedule_rejects_event_conflict(self):
        task = task_service.create_task(self.db, TaskCreate(title="整理资料"))
        event_service.create_event_series(
            self.db,
            EventSeriesCreate(
                title="会议",
                start_time=datetime(2026, 9, 18, 2),
                end_time=datetime(2026, 9, 18, 3),
                recurrence="weekly",
                occurrences=2,
            ),
        )
        with self.assertRaises(HTTPException) as caught:
            schedule_service.create_schedule(
                self.db,
                task.id,
                datetime(2026, 9, 18, 2, 30),
                datetime(2026, 9, 18, 3, 30),
            )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("固定日程「会议」", str(caught.exception.detail))
        self.assertIn("9月18日 10:00–11:00", str(caught.exception.detail))

    def test_schedule_move_conflict_names_schedule_and_local_time(self):
        first = task_service.create_task(self.db, TaskCreate(title="写报告"))
        second = task_service.create_task(self.db, TaskCreate(title="整理数据"))
        existing = schedule_service.create_schedule(
            self.db,
            first.id,
            datetime(2026, 9, 18, 2),
            datetime(2026, 9, 18, 3),
            status="accepted",
        )
        moving = schedule_service.create_schedule(
            self.db,
            second.id,
            datetime(2026, 9, 18, 4),
            datetime(2026, 9, 18, 5),
        )

        preview = schedule_service.preview_schedule_update(
            self.db,
            moving.id,
            ScheduleUpdate(
                start_time=datetime(2026, 9, 18, 2, 30),
                end_time=datetime(2026, 9, 18, 3, 30),
            ),
        )

        self.assertFalse(preview.valid)
        message = "；".join(preview.conflicts)
        self.assertIn("任务安排「写报告」", message)
        self.assertIn(f"#{existing.id}", message)
        self.assertIn("9月18日 10:00–11:00", message)
        self.assertIn("已确认", message)

    def test_task_completion_and_cancellation_sync_schedule(self):
        task = task_service.create_task(self.db, TaskCreate(title="实验"))
        schedule = Schedule(
            task_id=task.id,
            start_time=datetime(2026, 9, 17, 1),
            end_time=datetime(2026, 9, 17, 2),
            status="accepted",
        )
        self.db.add(schedule)
        self.db.commit()

        task_service.update_task(self.db, task.id, TaskUpdate(status="completed"))
        self.db.refresh(schedule)
        self.assertEqual(schedule.status, "completed")
