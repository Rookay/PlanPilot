# -*- coding: UTF-8 -*-
"""测试范围风险分析、计划质量审计与执行后复查。

从原先 1412 行、33 个用例挤在一个 ServiceTests 类里拆出来：
失败时能直接看出是哪个域坏了，也可以单独跑一个文件。
"""

from datetime import datetime
from unittest import TestCase
from unittest.mock import patch
from app.models.schedule import Schedule
from app.schemas.event import EventCreate
from app.integrations.maps.schemas import RouteOption, RouteResponse
from app.schemas.schedule import ScheduleGenerateRequest
from app.schemas.task import TaskCreate
from app.services import event_service, schedule_service, task_service
from tests.helpers import make_session


class PlanRiskAuditTests(TestCase):
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
