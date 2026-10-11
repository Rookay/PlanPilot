import json
from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

from app.agent.confirmation import PendingActionStore
from app.agent.agent import Agent
from app.agent.loop import AgentLoop
from app.agent.memory import Memory
from app.llm.mock import MockProvider
from app.integrations.maps.base import MapNotConfiguredError
from app.integrations.maps.schemas import RouteOption, RouteResponse
from app.schemas.schedule import ScheduleGenerateRequest
from app.schemas.event import EventCreate
from app.schemas.task import TaskCreate
from app.services import event_service, schedule_service, task_service
from app.skills.planner_skills import build_default_skills
from app.tools.defaults import build_default_registry
from tests.helpers import make_session


class AgentLoopTests(TestCase):
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

    def test_iteration_limit_uses_one_tool_free_final_summary(self):
        class OneIterationLoop(AgentLoop):
            MAX_ITERATIONS = 1

        provider = MockProvider(
            script=[
                {
                    "tool_calls": [
                        {"name": "get_current_time", "arguments": {}}
                    ]
                },
                {"text": "已根据当前时间完成总结。"},
            ]
        )
        loop = OneIterationLoop(
            llm=provider,
            registry=build_default_registry(),
            memory=Memory(),
            confirmation_store=PendingActionStore(),
            skill_registry=build_default_skills(),
        )

        result = loop.run(self.db, "现在适合做什么", "limit-summary")

        self.assertEqual(result.reply, "已根据当前时间完成总结。")
        self.assertEqual([item["name"] for item in result.tool_calls], ["get_current_time"])

    def test_agent_compacts_history_and_route_geometry(self):
        loop = AgentLoop(
            llm=MockProvider(),
            registry=build_default_registry(),
            memory=Memory(),
            confirmation_store=PendingActionStore(),
        )
        compacted = loop._compact_history(
            [{"role": "assistant", "content": "A" * 3000}]
        )
        self.assertLess(len(compacted[0]["content"]), 1900)
        self.assertIn("较长历史内容已压缩", compacted[0]["content"])

        message = loop._tool_message(
            "route-call",
            "estimate_travel_time",
            True,
            data={"routes": [{"path": [{"longitude": index} for index in range(100)]}]},
        )
        payload = json.loads(message["content"])
        path = payload["data"]["routes"][0]["path"]
        self.assertEqual(path["point_count"], 100)
        self.assertEqual(path["start"]["longitude"], 0)
        self.assertEqual(path["end"]["longitude"], 99)

    def test_saturated_tool_is_removed_from_next_model_turn(self):
        loop = AgentLoop(
            llm=MockProvider(),
            registry=build_default_registry(),
            memory=Memory(),
            confirmation_store=PendingActionStore(),
        )

        schemas = loop._available_tool_schemas({"get_weather", "get_events"})
        names = {item["function"]["name"] for item in schemas}

        self.assertNotIn("get_weather", names)
        self.assertNotIn("get_events", names)
        self.assertIn("get_current_time", names)

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
            self.db,
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

    def test_date_comparison_selects_compact_recommendation_skill(self):
        skill = build_default_skills().select("下周我想开车回家，哪天比较合适")

        self.assertIsNotNone(skill)
        self.assertEqual(skill.name, "date_recommendation")
        self.assertIn("禁止逐日查询", skill.system_hint)
        self.assertIn("get_weather", skill.allowed_tools)
        self.assertNotIn("generate_plan", skill.allowed_tools)

        repeated = build_default_skills().select(
            "下周我想开车回家，哪天比较合适，重新安排，不参考之前的"
        )
        self.assertEqual(repeated.name, "date_recommendation")

    def test_plan_health_skill_uses_read_only_risk_tool(self):
        skill = build_default_skills().select("看看我这周的计划有没有风险，任务来得及吗")

        self.assertIsNotNone(skill)
        self.assertEqual(skill.name, "plan_health")
        self.assertIn("analyze_plan_risks", skill.allowed_tools)
        tool = build_default_registry().get("analyze_plan_risks")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.confirmation, "none")

    def test_plan_alternatives_skill_requires_choice_before_apply(self):
        skill = build_default_skills().select("给我几套方案对比一下，不要直接应用")

        self.assertIsNotNone(skill)
        self.assertEqual(skill.name, "plan_alternatives")
        self.assertIn("compare_plan_alternatives", skill.allowed_tools)
        self.assertIn("apply_plan_alternative", skill.allowed_tools)
        registry = build_default_registry()
        self.assertEqual(registry.get("compare_plan_alternatives").confirmation, "none")
        self.assertEqual(registry.get("apply_plan_alternative").confirmation, "confirm")

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
                "travel_mode": "transit",
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

        with patch(
            "app.tools.event_tools.get_map_service",
            side_effect=MapNotConfiguredError("not configured"),
        ):
            pending = agent.handle(self.db, "安排马鞍山第一天行程", "trip-session")

        self.assertTrue(pending.confirmation.required)
        self.assertEqual(len(pending.confirmation.preview["events"]), 3)
        self.assertEqual(event_service.list_events(self.db), [])
        with patch(
            "app.agent.agent.schedule_service.audit_applied_changes",
            return_value={
                "valid": True,
                "issues": [],
                "warnings": [],
                "audited": True,
            },
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
        self.assertEqual(created[1].travel_mode, "transit")

    def test_travel_itinerary_preview_rejects_short_route_before_confirmation(self):
        class FakeMapService:
            def get_routes(self, origin, destination, mode, city):
                return RouteResponse(
                    provider="fake",
                    origin=origin,
                    destination=destination,
                    routes=[RouteOption(distance_m=8000, duration_minutes=30, mode=mode)],
                )

        with patch(
            "app.tools.event_tools.get_map_service",
            return_value=FakeMapService(),
        ):
            preview = build_default_registry().preview(
                "create_events",
                {
                    "events": [
                        {
                            "title": "午饭",
                            "start_time": "2026-09-27T03:00:00Z",
                            "end_time": "2026-09-27T04:00:00Z",
                            "event_type": "activity",
                            "location": "金鹰购物中心",
                            "location_city": "马鞍山",
                            "location_longitude": 118.50,
                            "location_latitude": 31.69,
                        },
                        {
                            "title": "前往采石矶",
                            "start_time": "2026-09-27T04:00:00Z",
                            "end_time": "2026-09-27T04:20:00Z",
                            "event_type": "travel",
                            "travel_mode": "driving",
                            "location": "采石矶",
                            "location_city": "马鞍山",
                            "location_longitude": 118.42,
                            "location_latitude": 31.65,
                        },
                    ]
                },
                self.db,
            )

        self.assertFalse(preview.success)
        self.assertIn("迟到 10 分钟", preview.error)

    def test_mock_fallback_keeps_recent_trip_context(self):
        response = MockProvider().chat(
            [
                {"role": "user", "content": "安排马鞍山第一天行程"},
                {
                    "role": "assistant",
                    "content": "已创建 11 段行程，复查发现固定行程冲突，会迟到 21 分钟。",
                },
                {"role": "user", "content": "那调整一下时间，不要迟到"},
            ],
            tools=[],
        )

        self.assertIn("刚才的旅行行程", response.text)
        self.assertIn("重新连接", response.text)
        self.assertNotIn("可用工具", response.text)

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

    def test_trip_revision_uses_one_confirmation_and_one_atomic_tool(self):
        events = event_service.create_events(
            self.db,
            [
                EventCreate(
                    title=f"行程{index + 1}",
                    start_time=datetime(2026, 9, 27, 2 + index),
                    end_time=datetime(2026, 9, 27, 3 + index),
                )
                for index in range(3)
            ],
            trip_title="马鞍山第一天",
        )
        provider = MockProvider(
            script=[
                {
                    "tool_calls": [
                        {
                            "name": "revise_trip",
                            "arguments": {
                                "trip_id": events[0].trip_id,
                                "shift_minutes": 30,
                            },
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

        pending = agent.handle(self.db, "整体顺延半小时", "trip-revision")

        self.assertTrue(pending.confirmation.required)
        self.assertEqual([item["name"] for item in pending.tool_calls], ["revise_trip"])
        completed = agent.handle(
            self.db,
            "",
            "trip-revision",
            confirm_action_id=pending.confirmation.action_id,
            decision="accept",
        )
        self.assertEqual([item["name"] for item in completed.tool_calls], ["revise_trip"])
        shifted = event_service.get_trip_events(self.db, events[0].trip_id)
        self.assertEqual(shifted[0].start_time, datetime(2026, 9, 27, 2, 30))
