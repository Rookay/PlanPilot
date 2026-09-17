from datetime import datetime
from unittest import TestCase

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
