from datetime import datetime, timezone
from unittest import TestCase
import warnings

from fastapi import FastAPI

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated",
)
from fastapi.testclient import TestClient

from app.api import agent, events, locations, preferences, schedules, tasks
from app.database import get_db
from tests.helpers import make_session


class ApiWorkflowTests(TestCase):
    def setUp(self):
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

    def test_agent_history_is_persisted(self):
        session_id = "api-history-test"
        response = self.client.post(
            "/api/agent/chat",
            json={"message": "看看我的任务", "session_id": session_id},
        )
        self.assertEqual(response.status_code, 200, response.text)
        history = self.client.get(
            f"/api/agent/sessions/{session_id}/messages"
        ).json()
        self.assertEqual([item["role"] for item in history], ["user", "assistant"])

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
