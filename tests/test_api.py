from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import patch
import warnings

from fastapi import FastAPI

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated",
)
from fastapi.testclient import TestClient

from app.api import agent, events, locations, preferences, schedules, tasks
from app.database import get_db
from app.schemas.event import EventCreate
from app.schemas.task import TaskCreate
from app.services import event_service, schedule_service, task_service
from tests.helpers import make_session


class ApiWorkflowTests(TestCase):
    def setUp(self):
        self.time_patcher = patch(
            "app.services.time_policy.utcnow",
            return_value=datetime(2026, 9, 15),
        )
        self.time_patcher.start()
        self.db = make_session()
        application = FastAPI()
        for router in (
            tasks.router,
            events.router,
            schedules.router,
            preferences.router,
            locations.router,
            agent.router,
        ):
            application.include_router(router)

        def _db_override():
            yield self.db

        application.dependency_overrides[get_db] = _db_override
        self.client = TestClient(application)

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.time_patcher.stop()

    def test_task_plan_and_batch_accept_workflow(self):
        preference_response = self.client.put(
            "/api/preferences",
            json={
                "timezone": "Asia/Shanghai",
                "workday_start": "09:00",
                "workday_end": "18:00",
                "lunch_start": "12:00",
                "lunch_end": "13:00",
                "default_gap_minutes": 20,
            },
        )
        self.assertEqual(preference_response.status_code, 200)

        task_response = self.client.post(
            "/api/tasks",
            json={"title": "完成论文", "estimated_duration": 90, "priority": "high"},
        )
        self.assertEqual(task_response.status_code, 200)

        start = datetime(2026, 9, 16, tzinfo=timezone.utc).isoformat()
        plan_response = self.client.post(
            "/api/schedules/generate",
            json={"start": start, "days": 2},
        )
        self.assertEqual(plan_response.status_code, 200, plan_response.text)
        plan = plan_response.json()
        self.assertEqual(plan["analysis"]["scheduled_count"], 1)
        self.assertEqual(plan["schedules"][0]["status"], "proposed")

        accept_response = self.client.post(
            "/api/schedules/accept-all",
            params={"batch_id": plan["batch_id"]},
        )
        self.assertEqual(accept_response.json()["accepted"], 1)

        risk_response = self.client.get(
            "/api/schedules/risks",
            params={
                "start": "2026-09-16T00:00:00Z",
                "end": "2026-09-18T00:00:00Z",
            },
        )
        self.assertEqual(risk_response.status_code, 200, risk_response.text)
        self.assertIn("health_score", risk_response.json())

    def test_agent_history_is_persisted(self):
        session_id = "api-history-test"
        response = self.client.post(
            "/api/agent/chat",
            json={"message": "看看我的任务", "session_id": session_id},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["run_id"])
        self.assertGreaterEqual(payload["duration_ms"], 0)
        self.assertTrue(payload["workflow_steps"])
        history = self.client.get(
            f"/api/agent/sessions/{session_id}/messages"
        ).json()
        self.assertEqual([item["role"] for item in history], ["user", "assistant"])
        self.assertEqual(history[-1]["agent_run_id"], payload["run_id"])
        runs = self.client.get(
            f"/api/agent/sessions/{session_id}/runs"
        ).json()
        self.assertEqual(runs[0]["id"], payload["run_id"])
        self.assertTrue(runs[0]["steps"])

    def test_saved_locations_keep_exactly_one_default(self):
        home = self.client.post(
            "/api/locations",
            json={
                "label": "家",
                "address": "人民大道 1 号",
                "city": "上海",
                "longitude": 121.4737,
                "latitude": 31.2304,
            },
        )
        self.assertEqual(home.status_code, 201, home.text)
        self.assertTrue(home.json()["is_default"])

        campus = self.client.post(
            "/api/locations",
            json={
                "label": "学校",
                "address": "校园路 10 号",
                "city": "上海",
                "longitude": 121.501,
                "latitude": 31.281,
            },
        )
        self.assertEqual(campus.status_code, 201, campus.text)
        campus_id = campus.json()["id"]

        switched = self.client.put(
            f"/api/locations/{campus_id}", json={"is_default": True}
        )
        self.assertTrue(switched.json()["is_default"])
        listed = self.client.get("/api/locations").json()
        self.assertEqual(sum(item["is_default"] for item in listed), 1)
        self.assertEqual(listed[0]["label"], "学校")

        self.client.delete(f"/api/locations/{campus_id}")
        remaining = self.client.get("/api/locations").json()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["label"], "家")
        self.assertTrue(remaining[0]["is_default"])

    def test_group_delete_endpoints_remove_only_the_selected_group(self):
        grouped = event_service.create_events(
            self.db,
            [
                EventCreate(
                    title=f"连续日程 {index + 1}",
                    start_time=datetime(2026, 9, 20, 1 + index),
                    end_time=datetime(2026, 9, 20, 2 + index),
                )
                for index in range(2)
            ],
            trip_title="两段安排",
        )
        deleted_trip = self.client.delete(f"/api/events/trips/{grouped[0].trip_id}")
        self.assertEqual(deleted_trip.status_code, 200, deleted_trip.text)
        self.assertEqual(deleted_trip.json()["deleted"], 2)

        task = task_service.create_task(self.db, TaskCreate(title="拆分任务"))
        schedule_service.create_schedule(
            self.db, task.id, datetime(2026, 9, 21, 1), datetime(2026, 9, 21, 2)
        )
        schedule_service.create_schedule(
            self.db, task.id, datetime(2026, 9, 21, 3), datetime(2026, 9, 21, 4)
        )
        deleted_schedules = self.client.delete(f"/api/schedules/task/{task.id}")
        self.assertEqual(deleted_schedules.status_code, 200, deleted_schedules.text)
        self.assertEqual(deleted_schedules.json()["deleted"], 2)
        self.assertIsNotNone(task_service.get_task(self.db, task.id))

    def test_plan_versions_lock_and_undo_work_through_api(self):
        task = self.client.post(
            "/api/tasks",
            json={
                "title": "可拆分项目",
                "estimated_duration": 120,
                "splittable": True,
                "min_chunk_minutes": 30,
                "max_chunk_minutes": 60,
            },
        )
        self.assertEqual(task.status_code, 200, task.text)
        plan = self.client.post(
            "/api/schedules/generate",
            json={
                "start": "2026-09-16T01:00:00Z",
                "days": 1,
                "windows": [
                    {
                        "start": "2026-09-16T01:00:00Z",
                        "end": "2026-09-16T06:00:00Z",
                    }
                ],
                "gap_minutes": 0,
            },
        )
        self.assertEqual(plan.status_code, 200, plan.text)
        self.assertEqual(len(plan.json()["schedules"]), 2)
        schedule_id = plan.json()["schedules"][0]["id"]

        locked = self.client.patch(
            f"/api/schedules/{schedule_id}",
            json={"is_locked": True},
        )
        self.assertEqual(locked.status_code, 200, locked.text)
        self.assertTrue(locked.json()["is_locked"])

        versions = self.client.get("/api/schedules/versions").json()
        self.assertGreaterEqual(len(versions), 2)
        undone = self.client.post("/api/schedules/versions/undo-latest")
        self.assertEqual(undone.status_code, 200, undone.text)
        schedules = self.client.get("/api/schedules").json()
        restored = next(item for item in schedules if item["id"] == schedule_id)
        self.assertFalse(restored["is_locked"])
