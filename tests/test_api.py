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

    def test_plan_alternatives_preview_and_apply_endpoints(self):
        for index in range(2):
            self.client.post(
                "/api/tasks",
                json={"title": f"方案任务 {index + 1}", "estimated_duration": 60},
            )
        payload = {
            "start": "2026-09-16T01:00:00Z",
            "days": 1,
            "gap_minutes": 15,
        }

        preview = self.client.post("/api/schedules/alternatives", json=payload)

        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(len(preview.json()["alternatives"]), 3)
        recommended = next(
            item for item in preview.json()["alternatives"] if item["recommended"]
        )
        self.assertTrue(recommended["recommendation_reason"])
        self.assertTrue(recommended["strengths"])
        self.assertTrue(recommended["tradeoffs"])
        self.assertEqual(self.client.get("/api/schedules").json(), [])

        applied = self.client.post(
            "/api/schedules/alternatives/balanced/apply",
            json=payload,
        )
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(len(applied.json()["schedules"]), 2)

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


class SessionListingTests(TestCase):
    """会话列表与删除。

    补的是前端"看不到历史"的那个洞：数据一直在 ``chat_messages`` 里，缺的是
    一个能列出所有会话的入口。
    """

    def setUp(self):
        self.time_patcher = patch(
            "app.services.time_policy.utcnow",
            return_value=datetime(2026, 9, 15),
        )
        self.time_patcher.start()
        self.db = make_session()
        application = FastAPI()
        application.include_router(agent.router)

        def _db_override():
            yield self.db

        application.dependency_overrides[get_db] = _db_override
        self.client = TestClient(application)

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.time_patcher.stop()

    def _add(self, session_id, role, content, day):
        from datetime import datetime as dt

        from app.models.chat import ChatMessage

        self.db.add(
            ChatMessage(
                session_id=session_id,
                role=role,
                content=content,
                created_at=dt(2026, 9, day, 10, 0, 0),
            )
        )
        self.db.commit()

    def test_orders_by_recent_activity(self):
        self._add("older", "user", "上个月的问题", 1)
        self._add("newer", "user", "昨天的问题", 5)
        self._add("older", "assistant", "上个月的回答", 2)

        sessions = self.client.get("/api/agent/sessions").json()

        self.assertEqual([item["session_id"] for item in sessions], ["newer", "older"])

    def test_title_uses_first_user_message_and_preview_uses_last(self):
        self._add("s1", "user", "第一个问题", 1)
        self._add("s1", "assistant", "已为你处理", 2)
        self._add("s1", "user", "第二个问题", 3)

        session = self.client.get("/api/agent/sessions").json()[0]

        # 助手回复常以"已为你…"开头，第一句人话更能代表会话在聊什么。
        self.assertEqual(session["title"], "第一个问题")
        self.assertEqual(session["preview"], "第二个问题")
        self.assertEqual(session["message_count"], 3)

    def test_session_without_user_message_still_listed(self):
        self._add("legacy", "assistant", "只有助手消息", 1)

        session = self.client.get("/api/agent/sessions").json()[0]

        self.assertEqual(session["title"], "（无用户消息）")
        self.assertEqual(session["message_count"], 1)

    def test_limit_caps_result(self):
        for index in range(3):
            self._add(f"s{index}", "user", f"问题{index}", index + 1)

        sessions = self.client.get("/api/agent/sessions?limit=2").json()

        self.assertEqual(len(sessions), 2)

    def test_delete_session_removes_it_from_list(self):
        self._add("keep", "user", "保留", 1)
        self._add("drop", "user", "删掉", 2)
        self._add("drop", "assistant", "回复", 3)

        response = self.client.delete("/api/agent/sessions/drop")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted"], 2)
        remaining = [item["session_id"] for item in self.client.get("/api/agent/sessions").json()]
        self.assertEqual(remaining, ["keep"])

    def test_empty_database_returns_empty_list(self):
        self.assertEqual(self.client.get("/api/agent/sessions").json(), [])
