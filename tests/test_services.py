from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

from app.models.schedule import Schedule
from fastapi import HTTPException
from app.agent.context import build_world_summary
from app.schemas.event import EventCreate, EventSeriesCreate, EventUpdate, TripRevision
from app.schemas.location import SavedLocationCreate
from app.integrations.maps.schemas import RouteOption, RouteResponse
from app.integrations.maps.base import MapNotConfiguredError
from app.schemas.preference import PreferenceUpdate
from app.schemas.schedule import LocalReplanRequest, ScheduleGenerateRequest, ScheduleUpdate
from app.schemas.task import TaskCreate, TaskUpdate
from app.services import (
    event_service,
    location_service,
    preference_service,
    schedule_service,
    task_service,
    itinerary_service,
)
from tests.helpers import make_session


class ServiceTests(TestCase):
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

    def test_plan_risk_report_is_scoped_and_actionable(self):
        urgent = task_service.create_task(
            self.db,
            TaskCreate(
                title="临近截止任务",
                estimated_duration=180,
                deadline=datetime(2026, 9, 16, 10),
            ),
        )
        event_service.create_event(
            self.db,
            EventCreate(
                title="范围内会议",
                start_time=datetime(2026, 9, 16, 1),
                end_time=datetime(2026, 9, 16, 3),
            ),
        )
        event_service.create_event(
            self.db,
            EventCreate(
                title="范围外会议",
                start_time=datetime(2026, 9, 20, 1),
                end_time=datetime(2026, 9, 20, 2),
            ),
        )

        report = schedule_service.analyze_plan_risks(
            self.db,
            datetime(2026, 9, 16),
            datetime(2026, 9, 17),
        )

        self.assertTrue(report.can_replan)
        self.assertLess(report.health_score, 100)
        deadline_risk = next(item for item in report.risks if item.code == "deadline_at_risk")
        self.assertEqual(deadline_risk.task_ids, [urgent.id])
        self.assertFalse(any("范围外会议" in item.detail for item in report.risks))

    def test_plan_risk_report_detects_current_range_conflict(self):
        first = task_service.create_task(
            self.db,
            TaskCreate(title="第一项", estimated_duration=60),
        )
        schedule = Schedule(
            task_id=first.id,
            start_time=datetime(2026, 9, 16, 2),
            end_time=datetime(2026, 9, 16, 4),
            status="proposed",
        )
        self.db.add(schedule)
        self.db.commit()
        event_service.create_event(
            self.db,
            EventCreate(
                title="冲突会议",
                start_time=datetime(2026, 9, 16, 3),
                end_time=datetime(2026, 9, 16, 5),
            ),
        )

        report = schedule_service.analyze_plan_risks(
            self.db,
            datetime(2026, 9, 16),
            datetime(2026, 9, 17),
        )

        conflict = next(item for item in report.risks if item.code == "time_conflict")
        self.assertEqual(conflict.severity, "critical")
        self.assertEqual(conflict.schedule_ids, [schedule.id])

    def test_fixed_only_overload_does_not_offer_task_replan(self):
        for index, (start, end) in enumerate(((1, 7), (7, 12))):
            event_service.create_event(
                self.db,
                EventCreate(
                    title=f"固定安排 {index + 1}",
                    start_time=datetime(2026, 9, 16, start),
                    end_time=datetime(2026, 9, 16, end),
                ),
            )

        report = schedule_service.analyze_plan_risks(
            self.db,
            datetime(2026, 9, 16),
            datetime(2026, 9, 17),
        )

        self.assertFalse(report.can_replan)
        load_risk = next(item for item in report.risks if item.code == "daily_overload")
        self.assertEqual(load_risk.action, "review")
        self.assertIn("固定日程", load_risk.detail)

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

    def test_batch_events_are_atomic_when_times_overlap(self):
        items = [
            EventCreate(
                title="第一段",
                start_time=datetime(2026, 9, 27, 2),
                end_time=datetime(2026, 9, 27, 3),
            ),
            EventCreate(
                title="重叠段",
                start_time=datetime(2026, 9, 27, 2, 30),
                end_time=datetime(2026, 9, 27, 4),
            ),
        ]

        with self.assertRaises(HTTPException):
            event_service.create_events(self.db, items)

        self.assertEqual(event_service.list_events(self.db), [])

    def test_trip_group_revision_shifts_all_events_atomically(self):
        events = event_service.create_events(
            self.db,
            [
                EventCreate(
                    title=f"第{index + 1}段",
                    start_time=datetime(2026, 9, 27, 2 + index),
                    end_time=datetime(2026, 9, 27, 3 + index),
                )
                for index in range(3)
            ],
            trip_title="马鞍山第一天",
        )
        trip_id = events[0].trip_id
        original = {event.id: event.start_time for event in events}

        revised = event_service.apply_trip_revision(
            self.db,
            TripRevision(
                trip_id=trip_id,
                shift_minutes=60,
                updates=[{"event_id": events[1].id, "title": "调整后的第二段"}],
            ),
        )

        self.assertEqual({item.trip_id for item in revised}, {trip_id})
        self.assertEqual([item.trip_order for item in revised], [1, 2, 3])
        self.assertEqual(revised[1].title, "调整后的第二段")
        self.assertTrue(
            all(
                (item.start_time - original[item.id]).total_seconds() == 3600
                for item in revised
            )
        )

        with self.assertRaises(HTTPException):
            event_service.apply_trip_revision(
                self.db,
                TripRevision(
                    trip_id=trip_id,
                    updates=[
                        {
                            "event_id": revised[2].id,
                            "start_time": revised[0].start_time,
                        }
                    ],
                ),
            )
        unchanged = event_service.get_trip_events(self.db, trip_id)
        self.assertEqual([item.start_time for item in unchanged], [item.start_time for item in revised])

    def test_whole_trip_group_can_be_deleted_atomically(self):
        grouped = event_service.create_events(
            self.db,
            [
                EventCreate(
                    title=f"组内第 {index + 1} 段",
                    start_time=datetime(2026, 9, 20, 1 + index),
                    end_time=datetime(2026, 9, 20, 2 + index),
                )
                for index in range(3)
            ],
            trip_title="周末安排",
        )
        standalone = event_service.create_event(
            self.db,
            EventCreate(
                title="独立日程",
                start_time=datetime(2026, 9, 21, 1),
                end_time=datetime(2026, 9, 21, 2),
            ),
        )

        result = event_service.delete_trip(self.db, grouped[0].trip_id)

        self.assertEqual(result["deleted"], 3)
        self.assertEqual(result["trip_title"], "周末安排")
        self.assertEqual([item.id for item in event_service.list_events(self.db)], [standalone.id])

    def test_planning_reserves_amap_travel_time_between_locations(self):
        event_service.create_event(
            self.db,
            EventCreate(
                title="校内会议",
                start_time=datetime(2026, 9, 16, 9),
                end_time=datetime(2026, 9, 16, 10),
                location="将军路校区",
                location_city="南京",
                location_longitude=118.78,
                location_latitude=31.94,
            ),
        )
        task_service.create_task(
            self.db,
            TaskCreate(
                title="新街口办事",
                estimated_duration=60,
                earliest_start=datetime(2026, 9, 16, 10),
                location="新街口",
                location_city="南京",
                location_longitude=118.79,
                location_latitude=32.04,
            ),
        )

        class FakeMapService:
            def get_routes(self, origin, destination, mode, city):
                return RouteResponse(
                    provider="fake",
                    origin=origin,
                    destination=destination,
                    routes=[
                        RouteOption(
                            distance_m=15000,
                            duration_minutes=60,
                            mode=mode,
                        )
                    ],
                )

        payload = ScheduleGenerateRequest.model_validate(
            {
                "start": datetime(2026, 9, 16, 8),
                "days": 1,
                "windows": [
                    {
                        "start": datetime(2026, 9, 16, 8),
                        "end": datetime(2026, 9, 16, 15),
                    }
                ],
                "gap_minutes": 0,
            }
        )
        with patch(
            "app.services.schedule.core.get_map_service",
            return_value=FakeMapService(),
        ):
            result = schedule_service.generate_schedules(self.db, payload)

        self.assertEqual(result.schedules[0].start_time, datetime(2026, 9, 16, 11))
        self.assertIn("已用高德校验通勤", result.schedules[0].reason)

    def test_plan_analysis_ignores_unrelated_fixed_schedule_travel_conflict(self):
        for title, start, end, longitude in (
            ("午饭", 10, 11, 118.78),
            ("逛街", 11, 12, 118.90),
        ):
            event_service.create_event(
                self.db,
                EventCreate(
                    title=title,
                    start_time=datetime(2026, 9, 16, start),
                    end_time=datetime(2026, 9, 16, end),
                    location=title,
                    location_city="南京",
                    location_longitude=longitude,
                    location_latitude=32.0,
                ),
            )

        requested_routes = []

        class FakeMapService:
            def get_routes(self, origin, destination, mode, city):
                requested_routes.append((origin, destination, mode, city))
                return RouteResponse(
                    provider="fake",
                    origin=origin,
                    destination=destination,
                    routes=[
                        RouteOption(
                            distance_m=10000,
                            duration_minutes=35,
                            mode=mode,
                        )
                    ],
                )

        with patch(
            "app.services.schedule.core.get_map_service",
            return_value=FakeMapService(),
        ):
            result = schedule_service.generate_schedules(
                self.db,
                ScheduleGenerateRequest(start=datetime(2026, 9, 16, 8), days=1),
            )

        self.assertFalse(any("固定行程冲突" in issue for issue in result.analysis.issues))
        self.assertEqual(requested_routes, [])

    def test_plan_audit_uses_saved_travel_mode(self):
        event_service.create_event(
            self.db,
            EventCreate(
                title="午饭",
                start_time=datetime(2026, 9, 27, 3),
                end_time=datetime(2026, 9, 27, 4),
                location="金鹰购物中心",
                location_city="马鞍山",
                location_longitude=118.50,
                location_latitude=31.69,
            ),
        )
        event_service.create_event(
            self.db,
            EventCreate(
                title="前往采石矶",
                start_time=datetime(2026, 9, 27, 4),
                end_time=datetime(2026, 9, 27, 4, 25),
                event_type="travel",
                travel_mode="driving",
                location="采石矶",
                location_city="马鞍山",
                location_longitude=118.42,
                location_latitude=31.65,
            ),
        )
        requested_modes = []

        class FakeMapService:
            def get_routes(self, origin, destination, mode, city):
                requested_modes.append(mode)
                duration = 20 if mode == "driving" else 50
                return RouteResponse(
                    provider="fake",
                    origin=origin,
                    destination=destination,
                    routes=[RouteOption(distance_m=8000, duration_minutes=duration, mode=mode)],
                )

        with patch(
            "app.services.schedule.audit.get_map_service",
            return_value=FakeMapService(),
        ):
            result = schedule_service.audit_current_plan(self.db)

        self.assertTrue(result["valid"])
        self.assertEqual(requested_modes, ["driving"])

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

    def test_event_series_expands_weekdays(self):
        result = event_service.create_event_series(
            self.db,
            EventSeriesCreate(
                title="晨会",
                start_time=datetime(2026, 9, 18, 1),  # Friday
                end_time=datetime(2026, 9, 18, 1, 30),
                recurrence="weekdays",
                occurrences=3,
            ),
        )
        self.assertEqual([item.start_time.weekday() for item in result], [4, 0, 1])

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

    def test_saved_location_is_available_to_agent_context(self):
        location_service.create_location(
            self.db,
            SavedLocationCreate(
                label="家",
                address="人民大道 1 号",
                city="上海",
                longitude=121.4737,
                latitude=31.2304,
            ),
        )

        summary = build_world_summary(self.db)

        self.assertIn("家（默认出发点）", summary)
        self.assertIn("121.473700,31.230400", summary)

    def test_temporary_poi_is_included_in_itinerary(self):
        event_service.create_event(
            self.db,
            EventCreate(
                title="南京南站接同学",
                start_time=datetime(2026, 9, 16, 2),
                end_time=datetime(2026, 9, 16, 2, 30),
                location="南京南站南广场到达层",
                location_city="南京",
                location_longitude=118.7965,
                location_latitude=31.968,
            ),
        )
        with patch(
            "app.services.itinerary_service.get_map_service",
            side_effect=MapNotConfiguredError("not configured"),
        ):
            result = itinerary_service.get_daily_itinerary(
                self.db,
                datetime(2026, 9, 16).date(),
            )

        self.assertEqual(result.items[0].location.label, "南京南站南广场到达层")
        self.assertEqual(result.items[0].location.coordinate.longitude, 118.7965)

    def test_daily_itinerary_reports_travel_conflict(self):
        home = location_service.create_location(
            self.db,
            SavedLocationCreate(
                label="家",
                city="上海",
                longitude=121.47,
                latitude=31.23,
            ),
        )
        office = location_service.create_location(
            self.db,
            SavedLocationCreate(
                label="公司",
                city="上海",
                longitude=121.60,
                latitude=31.25,
            ),
        )
        event_service.create_event_series(
            self.db,
            EventSeriesCreate(
                title="早餐",
                start_time=datetime(2026, 9, 16, 1),
                end_time=datetime(2026, 9, 16, 2),
                location_id=home.id,
                recurrence="daily",
                occurrences=2,
            ),
        )
        first, second = event_service.list_events(self.db)[:2]
        event_service.update_event(
            self.db,
            second.id,
            EventUpdate(
                title="会议",
                start_time=datetime(2026, 9, 16, 2, 20),
                end_time=datetime(2026, 9, 16, 3, 20),
                location_id=office.id,
            ),
        )

        class FakeMapService:
            def get_routes(self, origin, destination, mode, city):
                return RouteResponse(
                    provider="fake",
                    origin=origin,
                    destination=destination,
                    routes=[
                        RouteOption(
                            distance_m=12000,
                            duration_minutes=40,
                            mode=mode,
                            path=[origin, destination],
                        )
                    ],
                )

        with patch(
            "app.services.itinerary_service.get_map_service",
            return_value=FakeMapService(),
        ):
            result = itinerary_service.get_daily_itinerary(
                self.db,
                datetime(2026, 9, 16).date(),
            )

        self.assertEqual([item.title for item in result.items], ["早餐", "会议"])
        self.assertEqual(result.legs[-1].duration_minutes, 40)
        self.assertIn("迟到 20 分钟", result.legs[-1].warning)

        with patch(
            "app.services.itinerary_service.get_map_service",
            return_value=FakeMapService(),
        ):
            with_return = itinerary_service.get_daily_itinerary(
                self.db,
                datetime(2026, 9, 16).date(),
                include_return=True,
            )
        self.assertEqual(with_return.legs[-1].destination_id, "origin-return")
        self.assertIsNone(with_return.legs[-1].warning)

    def test_itinerary_can_infer_or_select_trip_endpoints(self):
        school = location_service.create_location(
            self.db,
            SavedLocationCreate(
                label="学校",
                city="南京",
                longitude=118.80,
                latitude=31.90,
            ),
        )
        hotel = location_service.create_location(
            self.db,
            SavedLocationCreate(
                label="酒店",
                city="马鞍山",
                longitude=118.50,
                latitude=31.70,
            ),
        )
        station = location_service.create_location(
            self.db,
            SavedLocationCreate(
                label="马鞍山东站",
                city="马鞍山",
                longitude=118.55,
                latitude=31.70,
            ),
        )
        museum = location_service.create_location(
            self.db,
            SavedLocationCreate(
                label="马鞍山市博物馆",
                city="马鞍山",
                longitude=118.52,
                latitude=31.68,
            ),
        )
        event_service.create_event(
            self.db,
            EventCreate(
                title="到达马鞍山东站",
                start_time=datetime(2026, 9, 28, 2),
                end_time=datetime(2026, 9, 28, 2, 20),
                location_id=station.id,
            ),
        )
        event_service.create_event(
            self.db,
            EventCreate(
                title="参观博物馆",
                start_time=datetime(2026, 9, 28, 3),
                end_time=datetime(2026, 9, 28, 4),
                location_id=museum.id,
            ),
        )

        class FakeMapService:
            def get_routes(self, origin, destination, mode, city):
                return RouteResponse(
                    provider="fake",
                    origin=origin,
                    destination=destination,
                    routes=[
                        RouteOption(
                            distance_m=1000,
                            duration_minutes=10,
                            mode=mode,
                            path=[origin, destination],
                        )
                    ],
                )

        with patch(
            "app.services.itinerary_service.get_map_service",
            return_value=FakeMapService(),
        ):
            automatic = itinerary_service.get_daily_itinerary(
                self.db,
                datetime(2026, 9, 28).date(),
                origin_strategy="automatic",
            )
            selected = itinerary_service.get_daily_itinerary(
                self.db,
                datetime(2026, 9, 28).date(),
                origin_strategy="automatic",
                origin_location_id=hotel.id,
                destination_location_id=hotel.id,
            )

        self.assertEqual(automatic.origin.label, "马鞍山东站")
        self.assertNotEqual(automatic.origin.id, school.id)
        self.assertEqual(len(automatic.legs), 1)
        self.assertEqual(selected.origin.label, "酒店")
        self.assertEqual(selected.destination.label, "酒店")
        self.assertEqual(selected.legs[0].origin_id, "origin")
        self.assertEqual(selected.legs[-1].destination_id, "trip-end")

    def test_postflight_audit_only_checks_changed_events_and_boundaries(self):
        created = []
        for title, start_hour, end_hour, longitude in (
            ("旧行程A", 1, 2, 118.40),
            ("旧行程B", 2, 3, 118.50),
            ("新行程A", 10, 11, 118.60),
            ("新行程B", 12, 13, 118.61),
        ):
            created.append(
                event_service.create_event(
                    self.db,
                    EventCreate(
                        title=title,
                        start_time=datetime(2026, 9, 28, start_hour),
                        end_time=datetime(2026, 9, 28, end_hour),
                        location=title,
                        location_city="马鞍山",
                        location_longitude=longitude,
                        location_latitude=31.70,
                    ),
                )
            )

        class FakeMapService:
            def get_routes(self, origin, destination, mode, city):
                return RouteResponse(
                    provider="fake",
                    origin=origin,
                    destination=destination,
                    routes=[
                        RouteOption(
                            distance_m=10000,
                            duration_minutes=90 if origin.longitude == 118.40 else 20,
                            mode=mode,
                        )
                    ],
                )

        with patch(
            "app.services.schedule.audit.get_map_service",
            return_value=FakeMapService(),
        ):
            global_audit = schedule_service.audit_current_plan(self.db)
            scoped_audit = schedule_service.audit_applied_changes(
                self.db,
                [
                    {
                        "name": "create_events",
                        "data": {
                            "events": [
                                {"id": created[2].id},
                                {"id": created[3].id},
                            ]
                        },
                    }
                ],
            )

        self.assertTrue(any("旧行程A" in issue for issue in global_audit["issues"]))
        self.assertTrue(scoped_audit["audited"])
        self.assertEqual(scoped_audit["issues"], [])

    def test_travel_event_uses_its_end_time_as_arrival_deadline(self):
        event_service.create_event(
            self.db,
            EventCreate(
                title="午饭",
                start_time=datetime(2026, 9, 16, 1),
                end_time=datetime(2026, 9, 16, 2),
                location="南京南站",
                location_city="南京",
                location_longitude=118.79,
                location_latitude=31.97,
            ),
        )
        event_service.create_event(
            self.db,
            EventCreate(
                title="前往新街口",
                start_time=datetime(2026, 9, 16, 2, 10),
                end_time=datetime(2026, 9, 16, 3, 10),
                event_type="travel",
                location="新街口",
                location_city="南京",
                location_longitude=118.78,
                location_latitude=32.04,
            ),
        )

        class FakeMapService:
            def get_routes(self, origin, destination, mode, city):
                return RouteResponse(
                    provider="fake",
                    origin=origin,
                    destination=destination,
                    routes=[
                        RouteOption(
                            distance_m=12000,
                            duration_minutes=40,
                            mode=mode,
                        )
                    ],
                )

        with patch(
            "app.services.itinerary_service.get_map_service",
            return_value=FakeMapService(),
        ):
            result = itinerary_service.get_daily_itinerary(
                self.db,
                datetime(2026, 9, 16).date(),
            )

        leg = result.legs[-1]
        self.assertEqual(result.items[-1].event_type, "travel")
        self.assertEqual(result.items[-1].origin_location.label, "南京南站")
        self.assertEqual(leg.suggested_departure, datetime(2026, 9, 16, 2, 10))
        self.assertEqual(leg.estimated_arrival, datetime(2026, 9, 16, 2, 50))
        self.assertEqual(leg.available_minutes, 60)
        self.assertIsNone(leg.warning)

    def test_travel_event_prefers_its_explicit_origin(self):
        event_service.create_event(
            self.db,
            EventCreate(
                title="上午活动",
                start_time=datetime(2026, 9, 16, 1),
                end_time=datetime(2026, 9, 16, 2),
                location="地点A",
                location_city="南京",
                location_longitude=118.70,
                location_latitude=32.00,
            ),
        )
        event_service.create_event(
            self.db,
            EventCreate(
                title="从车站前往学校",
                start_time=datetime(2026, 9, 16, 3),
                end_time=datetime(2026, 9, 16, 4),
                event_type="travel",
                origin_location="地点B",
                origin_location_city="南京",
                origin_location_longitude=118.80,
                origin_location_latitude=32.00,
                location="地点C",
                location_city="南京",
                location_longitude=118.90,
                location_latitude=32.00,
            ),
        )

        route_origins = []

        class FakeMapService:
            def get_routes(self, origin, destination, mode, city):
                route_origins.append(origin.longitude)
                return RouteResponse(
                    provider="fake",
                    origin=origin,
                    destination=destination,
                    routes=[
                        RouteOption(
                            distance_m=1000,
                            duration_minutes=20,
                            mode=mode,
                            path=[origin, destination],
                        )
                    ],
                )

        with patch(
            "app.services.itinerary_service.get_map_service",
            return_value=FakeMapService(),
        ):
            result = itinerary_service.get_daily_itinerary(
                self.db,
                datetime(2026, 9, 16).date(),
            )

        travel = result.items[-1]
        travel_leg = result.legs[-1]
        self.assertEqual(travel.origin_location.label, "地点B")
        self.assertTrue(route_origins)
        self.assertTrue(all(value == 118.80 for value in route_origins))
        self.assertEqual(travel_leg.origin_id, f"{travel.id}:origin")
        self.assertEqual(travel_leg.suggested_departure, travel.start_time)

    def test_itinerary_displays_taxi_while_using_driving_route(self):
        event_service.create_event(
            self.db,
            EventCreate(
                title="酒店寄存行李",
                start_time=datetime(2026, 9, 27, 2),
                end_time=datetime(2026, 9, 27, 2, 30),
                location="酒店",
                location_city="马鞍山",
                location_longitude=118.50,
                location_latitude=31.69,
            ),
        )
        event_service.create_event(
            self.db,
            EventCreate(
                title="打车前往金鹰",
                start_time=datetime(2026, 9, 27, 2, 30),
                end_time=datetime(2026, 9, 27, 2, 40),
                event_type="travel",
                travel_mode="taxi",
                location="金鹰购物中心",
                location_city="马鞍山",
                location_longitude=118.505,
                location_latitude=31.692,
            ),
        )
        requested_modes = []

        class FakeMapService:
            def get_routes(self, origin, destination, mode, city):
                requested_modes.append(mode)
                return RouteResponse(
                    provider="fake",
                    origin=origin,
                    destination=destination,
                    routes=[
                        RouteOption(
                            distance_m=500,
                            duration_minutes=7,
                            mode=mode,
                            path=[origin, destination],
                        )
                    ],
                )

        with patch(
            "app.services.itinerary_service.get_map_service",
            return_value=FakeMapService(),
        ):
            result = itinerary_service.get_daily_itinerary(
                self.db,
                datetime(2026, 9, 27).date(),
                mode="smart",
            )

        self.assertEqual(result.items[-1].travel_mode, "taxi")
        self.assertEqual(result.legs[-1].mode, "driving")
        self.assertEqual(result.legs[-1].display_mode, "taxi")
        self.assertEqual(result.legs[-1].parking_minutes, 0)
        self.assertIn("指定打车", result.legs[-1].recommendation_reason)
        self.assertEqual(requested_modes, ["driving"])

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

    def test_smart_itinerary_mixes_modes_without_teleporting_the_car(self):
        location_service.create_location(
            self.db,
            SavedLocationCreate(
                label="家",
                city="南京",
                longitude=118.00,
                latitude=32.00,
            ),
        )
        for title, start, longitude in (
            ("地点B", 2, 118.01),
            ("地点C", 4, 118.02),
            ("地点D", 6, 118.03),
        ):
            event_service.create_event(
                self.db,
                EventCreate(
                    title=title,
                    start_time=datetime(2026, 9, 16, start),
                    end_time=datetime(2026, 9, 16, start, 30),
                    location=title,
                    location_city="南京",
                    location_longitude=longitude,
                    location_latitude=32.00,
                ),
            )

        durations = {
            (118.00, 118.01, "driving"): 2,
            (118.00, 118.01, "transit"): 30,
            (118.00, 118.01, "walking"): 100,
            (118.01, 118.02, "driving"): 20,
            (118.01, 118.02, "transit"): 10,
            (118.01, 118.02, "walking"): 3,
            (118.02, 118.03, "driving"): 30,
            (118.02, 118.03, "transit"): 20,
            (118.02, 118.03, "walking"): 100,
            (118.02, 118.01, "walking"): 3,
            (118.01, 118.03, "driving"): 20,
        }

        class FakeMapService:
            def get_routes(self, origin, destination, mode, city):
                key = (
                    round(origin.longitude, 2),
                    round(destination.longitude, 2),
                    mode,
                )
                if key not in durations:
                    return RouteResponse(
                        provider="fake",
                        origin=origin,
                        destination=destination,
                        routes=[],
                    )
                duration = durations[key]
                return RouteResponse(
                    provider="fake",
                    origin=origin,
                    destination=destination,
                    routes=[
                        RouteOption(
                            distance_m=duration * 1000,
                            duration_minutes=duration,
                            mode=mode,
                            path=[origin, destination],
                        )
                    ],
                )

        with patch(
            "app.services.itinerary_service.get_map_service",
            return_value=FakeMapService(),
        ):
            result = itinerary_service.get_daily_itinerary(
                self.db,
                datetime(2026, 9, 16).date(),
                mode="smart",
                car_available=True,
                parking_minutes=5,
            )

        self.assertEqual(
            [leg.mode for leg in result.legs],
            ["driving", "walking", "driving"],
        )
        self.assertEqual(result.legs[0].duration_minutes, 7)
        self.assertEqual(result.legs[0].parking_minutes, 5)
        self.assertEqual(
            [leg.car_location_after for leg in result.legs],
            ["地点B", "地点B", "地点D"],
        )
        self.assertIn("车辆停在", result.legs[1].recommendation_reason)
        self.assertEqual(result.legs[2].duration_minutes, 28)
        self.assertIn("取车", result.legs[2].recommendation_reason)
