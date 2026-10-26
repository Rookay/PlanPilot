# -*- coding: UTF-8 -*-
"""测试固定事件的批量原子性与多段行程组。

从原先 1412 行、33 个用例挤在一个 ServiceTests 类里拆出来：
失败时能直接看出是哪个域坏了，也可以单独跑一个文件。
"""

from datetime import datetime
from unittest import TestCase
from unittest.mock import patch
from fastapi import HTTPException
from app.schemas.event import EventCreate, EventSeriesCreate, TripRevision
from app.services import event_service
from tests.helpers import make_session


class EventTripServiceTests(TestCase):
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
