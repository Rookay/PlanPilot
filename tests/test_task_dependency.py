# -*- coding: UTF-8 -*-
"""测试任务依赖与可拆分任务的分块生成。

从原先 1412 行、33 个用例挤在一个 ServiceTests 类里拆出来：
失败时能直接看出是哪个域坏了，也可以单独跑一个文件。
"""

from datetime import datetime
from unittest import TestCase
from unittest.mock import patch
from fastapi import HTTPException
from app.schemas.schedule import ScheduleGenerateRequest
from app.schemas.task import TaskCreate, TaskUpdate
from app.services import schedule_service, task_service
from tests.helpers import make_session


class TaskDependencyTests(TestCase):
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


    def test_task_dependencies_reject_cycles(self):
        first = task_service.create_task(self.db, TaskCreate(title="任务A"))
        second = task_service.create_task(
            self.db,
            TaskCreate(title="任务B", dependency_ids=[first.id]),
        )
        with self.assertRaises(HTTPException) as context:
            task_service.update_task(
                self.db,
                first.id,
                TaskUpdate(dependency_ids=[second.id]),
            )
        self.assertIn("循环", context.exception.detail)

    def test_service_generates_all_chunks_for_splittable_task(self):
        task = task_service.create_task(
            self.db,
            TaskCreate(
                title="分段任务",
                estimated_duration=180,
                splittable=True,
                min_chunk_minutes=30,
                max_chunk_minutes=60,
            ),
        )
        result = schedule_service.generate_schedules(
            self.db,
            ScheduleGenerateRequest(
                start=datetime(2026, 9, 16, 1),
                days=1,
                windows=[
                    {
                        "start": datetime(2026, 9, 16, 1),
                        "end": datetime(2026, 9, 16, 7),
                    }
                ],
                gap_minutes=0,
            ),
        )
        chunks = [item for item in result.schedules if item.task_id == task.id]
        self.assertEqual(len(chunks), 3)
        self.assertEqual([item.chunk_index for item in chunks], [1, 2, 3])
        self.assertTrue(result.version_id)
