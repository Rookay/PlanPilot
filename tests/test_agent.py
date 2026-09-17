from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

from app.agent.confirmation import PendingActionStore
from app.agent.agent import Agent
from app.agent.loop import AgentLoop
from app.agent.memory import Memory
from app.llm.mock import MockProvider
from app.schemas.schedule import ScheduleGenerateRequest
from app.schemas.event import EventCreate
from app.schemas.task import TaskCreate
from app.services import event_service, schedule_service, task_service
from app.skills.planner_skills import build_default_skills
from app.tools.defaults import build_default_registry
from tests.helpers import make_session


class AgentLoopTests(TestCase):
    def setUp(self):
        self.db = make_session()

    def tearDown(self):
        self.db.close()

    def test_urgent_paper_request_completes_multistep_flow_offline(self):
        paper = task_service.create_task(
            self.db,
            TaskCreate(title="论文实验", priority="medium", estimated_duration=60),
        )
        loop = AgentLoop(
            llm=MockProvider(),
            registry=build_default_registry(),
            memory=Memory(),
            confirmation_store=PendingActionStore(),
            skill_registry=build_default_skills(),
        )

        result = loop.run(
            self.db,
            "论文比较急，尽快安排，其余的安排到后面，留出空闲",
            "test-session",
        )

        names = [item["name"] for item in result.tool_calls]
        self.assertEqual(names, ["get_tasks", "update_task", "generate_plan"])
        self.db.refresh(paper)
        self.assertEqual(paper.priority, "high")
        self.assertTrue(result.proposed_schedules)
        self.assertNotIn("最大", result.reply)

    def test_batch_event_delete_requires_only_one_confirmation(self):
        events = [
            event_service.create_event(
                self.db,
                EventCreate(
                    title=title,
                    start_time=datetime(2026, 9, 17 + index, 5),
                    end_time=datetime(2026, 9, 17 + index, 6),
                ),
            )
            for index, title in enumerate(
                ["出去玩", "上海行程（第1天）", "上海行程（第2天）"]
            )
        ]
        provider = MockProvider(
            script=[
                {
                    "tool_calls": [
                        {
                            "name": "delete_events",
                            "arguments": {"event_ids": [item.id for item in events]},
                        }
                    ]
                }
            ]
        )
        agent = Agent(
            llm=provider,
            registry=build_default_registry(),
            memory=Memory(),
            confirmation_store=PendingActionStore(),
            skill_registry=build_default_skills(),
        )

        pending = agent.handle(self.db, "清空上海行程和出去玩", "batch-delete")

        self.assertTrue(pending.confirmation.required)
        self.assertIn("一次删除 3 个事件", pending.confirmation.summary)
        self.assertEqual(event_service.list_events(self.db), events)

        completed = agent.handle(
            self.db,
            "",
            "batch-delete",
            confirm_action_id=pending.confirmation.action_id,
            decision="accept",
        )

        self.assertIn("已删除 3 个事件", completed.reply)
        self.assertEqual(event_service.list_events(self.db), [])

    def test_confirmed_batch_rolls_back_when_a_later_tool_fails(self):
        event = event_service.create_event(
            self.db,
            EventCreate(
                title="不能只删一半",
                start_time=datetime(2026, 9, 20, 5),
                end_time=datetime(2026, 9, 20, 6),
            ),
        )
        agent = Agent(
            llm=MockProvider(),
            registry=build_default_registry(),
            memory=Memory(),
            confirmation_store=PendingActionStore(),
            skill_registry=build_default_skills(),
        )
        action_id = agent.confirmation_store.create(
            tool_calls=[
                {"name": "delete_event", "args": {"event_id": event.id}},
                {"name": "delete_event", "args": {"event_id": 99999}},
            ],
            summary="测试原子执行",
            session_id="rollback-session",
        )

        result = agent.handle(
            self.db,
            "",
            "rollback-session",
            confirm_action_id=action_id,
            decision="accept",
        )

        self.assertIn("已恢复修改前状态", result.reply)
        self.assertEqual(event_service.get_event(self.db, event.id).title, "不能只删一半")
        self.assertEqual(result.applied_changes, [])

    def test_offline_agent_can_request_latest_plan_undo(self):
        task_service.create_task(
            self.db,
            TaskCreate(title="可撤销的安排", estimated_duration=60),
        )
        schedule_service.generate_schedules(
            self.db,
            ScheduleGenerateRequest(start=datetime(2026, 9, 18), days=2),
        )
        agent = Agent(
            llm=MockProvider(),
            registry=build_default_registry(),
            memory=Memory(),
            confirmation_store=PendingActionStore(),
            skill_registry=build_default_skills(),
        )

        pending = agent.handle(self.db, "撤销刚才的计划", "undo-session")

        self.assertTrue(pending.confirmation.required)
        self.assertIn("撤销", pending.confirmation.summary)
        completed = agent.handle(
            self.db,
            "",
            "undo-session",
            confirm_action_id=pending.confirmation.action_id,
            decision="accept",
        )
        self.assertIn("已撤销计划版本", completed.reply)
        self.assertEqual(schedule_service.list_schedules(self.db), [])

    def test_travel_request_selects_travel_skill(self):
        skill = build_default_skills().select(
            "9月27日去马鞍山玩，先去酒店放行李，安排第一天行程"
        )

        self.assertIsNotNone(skill)
        self.assertEqual(skill.name, "travel_planning")
        self.assertIn("禁止调用 generate_plan", skill.system_hint)

    def test_travel_itinerary_is_confirmed_and_created_as_one_batch(self):
        events = [
            {
                "title": "抵达马鞍山东站",
                "start_time": "2026-09-27T02:00:00Z",
                "end_time": "2026-09-27T02:10:00Z",
                "event_type": "activity",
                "location": "马鞍山东站",
                "location_city": "马鞍山",
                "location_longitude": 118.55,
                "location_latitude": 31.70,
            },
            {
                "title": "前往酒店",
                "start_time": "2026-09-27T02:10:00Z",
                "end_time": "2026-09-27T02:30:00Z",
                "event_type": "travel",
                "origin_location": "马鞍山东站",
                "origin_location_city": "马鞍山",
                "origin_location_longitude": 118.55,
                "origin_location_latitude": 31.70,
                "location": "精沐精选酒店",
                "location_city": "马鞍山",
                "location_longitude": 118.51,
                "location_latitude": 31.68,
            },
            {
                "title": "酒店寄存行李",
                "start_time": "2026-09-27T02:30:00Z",
                "end_time": "2026-09-27T03:00:00Z",
                "event_type": "activity",
                "location": "精沐精选酒店",
                "location_city": "马鞍山",
                "location_longitude": 118.51,
                "location_latitude": 31.68,
            },
        ]
        agent = Agent(
            llm=MockProvider(
                script=[
                    {
                        "tool_calls": [
                            {"name": "create_events", "arguments": {"events": events}}
                        ]
                    }
                ]
            ),
            registry=build_default_registry(),
            memory=Memory(),
            confirmation_store=PendingActionStore(),
            skill_registry=build_default_skills(),
        )

        pending = agent.handle(self.db, "安排马鞍山第一天行程", "trip-session")

        self.assertTrue(pending.confirmation.required)
        self.assertEqual(len(pending.confirmation.preview["events"]), 3)
        self.assertEqual(event_service.list_events(self.db), [])
        with patch(
            "app.agent.agent.schedule_service.audit_current_plan",
            return_value={"valid": True, "issues": [], "warnings": []},
        ):
            completed = agent.handle(
                self.db,
                "",
                "trip-session",
                confirm_action_id=pending.confirmation.action_id,
                decision="accept",
            )

        self.assertIn("已创建 3 段行程", completed.reply)
        created = event_service.list_events(self.db)
        self.assertEqual([item.event_type for item in created], ["activity", "travel", "activity"])

    def test_travel_itinerary_preview_rejects_existing_conflict(self):
        event_service.create_event(
            self.db,
            EventCreate(
                title="已有安排",
                start_time=datetime(2026, 9, 27, 3),
                end_time=datetime(2026, 9, 27, 4),
            ),
        )
        preview = build_default_registry().preview(
            "create_events",
            {
                "events": [
                    {
                        "title": "计划游览",
                        "start_time": "2026-09-27T03:30:00Z",
                        "end_time": "2026-09-27T05:00:00Z",
                        "event_type": "activity",
                    }
                ]
            },
            self.db,
        )

        self.assertFalse(preview.success)
        self.assertIn("已有安排", preview.error)
