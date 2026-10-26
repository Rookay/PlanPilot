# -*- coding: UTF-8 -*-
"""测试日程可达性：地图路线、通勤预留、地点与交通方式。

从原先 1412 行、33 个用例挤在一个 ServiceTests 类里拆出来：
失败时能直接看出是哪个域坏了，也可以单独跑一个文件。
"""

from datetime import datetime
from unittest import TestCase
from unittest.mock import patch
from app.agent.context import build_world_summary
from app.schemas.event import EventCreate, EventSeriesCreate, EventUpdate
from app.schemas.location import SavedLocationCreate
from app.integrations.maps.schemas import RouteOption, RouteResponse
from app.integrations.maps.base import MapNotConfiguredError
from app.schemas.schedule import ScheduleGenerateRequest
from app.schemas.task import TaskCreate
from app.services import (
    event_service,
    location_service,
    schedule_service,
    task_service,
    itinerary_service,
)
from tests.helpers import make_session


class ItineraryMapTests(TestCase):
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
