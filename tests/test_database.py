from unittest import TestCase

from sqlalchemy import create_engine, inspect

from app.database import ensure_schema


class DatabaseMigrationTests(TestCase):
    def test_legacy_tables_receive_required_columns(self):
        engine = create_engine("sqlite://", future=True)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE tasks (id INTEGER PRIMARY KEY, title VARCHAR(200))"
            )
            connection.exec_driver_sql(
                "CREATE TABLE schedules (id INTEGER PRIMARY KEY, created_at DATETIME)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE events (id INTEGER PRIMARY KEY, title VARCHAR(200))"
            )
            connection.exec_driver_sql(
                "INSERT INTO schedules (id, created_at) VALUES (1, '2026-09-01 00:00:00')"
            )
            connection.exec_driver_sql(
                "CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, content TEXT)"
            )

        ensure_schema(engine)
        inspector = inspect(engine)
        task_columns = {item["name"] for item in inspector.get_columns("tasks")}
        schedule_columns = {item["name"] for item in inspector.get_columns("schedules")}
        event_columns = {item["name"] for item in inspector.get_columns("events")}
        chat_columns = {item["name"] for item in inspector.get_columns("chat_messages")}

        self.assertIn("start_time", task_columns)
        self.assertTrue(
            {"splittable", "min_chunk_minutes", "max_chunk_minutes"}
            <= task_columns
        )
        self.assertIn("location_id", task_columns)
        self.assertIn("location_id", event_columns)
        self.assertIn("event_type", event_columns)
        self.assertIn("travel_mode", event_columns)
        self.assertTrue({"trip_id", "trip_title", "trip_order"} <= event_columns)
        self.assertTrue(
            {"location", "location_city", "location_longitude", "location_latitude"}
            <= task_columns
        )
        self.assertTrue(
            {"location_city", "location_longitude", "location_latitude"}
            <= event_columns
        )
        self.assertTrue(
            {
                "origin_location",
                "origin_location_id",
                "origin_location_city",
                "origin_location_longitude",
                "origin_location_latitude",
            }
            <= event_columns
        )
        self.assertTrue({"plan_batch_id", "reason", "updated_at"} <= schedule_columns)
        self.assertTrue(
            {"plan_version_id", "is_locked", "chunk_index", "source"}
            <= schedule_columns
        )
        self.assertIn("session_id", chat_columns)
        self.assertIn("agent_run_id", chat_columns)
        with engine.connect() as connection:
            updated_at = connection.exec_driver_sql(
                "SELECT updated_at FROM schedules WHERE id = 1"
            ).scalar_one()
        self.assertIsNotNone(updated_at)

    def test_legacy_travel_mode_distinguishes_taxi_from_ambiguous_options(self):
        engine = create_engine("sqlite://", future=True)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE events ("
                "id INTEGER PRIMARY KEY, title VARCHAR(200), description TEXT, "
                "event_type VARCHAR(20))"
            )
            connection.exec_driver_sql(
                "INSERT INTO events VALUES "
                "(1, '打车前往景区', '约 20 分钟', 'travel'), "
                "(2, '前往公园', '公交约 50 分钟；打车约 25 分钟', 'travel')"
            )

        ensure_schema(engine)

        with engine.connect() as connection:
            modes = connection.exec_driver_sql(
                "SELECT travel_mode FROM events ORDER BY id"
            ).scalars().all()
        self.assertEqual(modes, ["taxi", "transit"])

    def test_legacy_multi_event_day_becomes_an_ordered_trip_group(self):
        engine = create_engine("sqlite://", future=True)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE events ("
                "id INTEGER PRIMARY KEY, title VARCHAR(200), "
                "start_time DATETIME, event_type VARCHAR(20))"
            )
            connection.exec_driver_sql(
                "INSERT INTO events VALUES "
                "(1, '第一段', '2026-09-27 02:00:00', 'activity'), "
                "(2, '第二段', '2026-09-27 03:00:00', 'travel'), "
                "(3, '第三段', '2026-09-27 04:00:00', 'activity')"
            )

        ensure_schema(engine)

        with engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT trip_id, trip_order FROM events ORDER BY id"
            ).all()
        self.assertEqual({row[0] for row in rows}, {"legacy-2026-09-27"})
        self.assertEqual([row[1] for row in rows], [1, 2, 3])
