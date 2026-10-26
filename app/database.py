# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：database.py
import os
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import declarative_base, sessionmaker

# 项目根目录（app/ 的上一级）。
BASE_DIR = Path(__file__).resolve().parent.parent


def resolve_database_url() -> str:
    """解析数据库连接串。

    优先级：
    1. 环境变量 ``DATABASE_URL``（完整连接串，可换成 MySQL/PostgreSQL 等）
    2. 环境变量 ``DB_PATH``（SQLite 文件路径，支持相对/绝对/~）
    3. 项目根目录下的 ``schedule.db``
    """

    url = os.getenv("DATABASE_URL")
    if url:
        return url

    db_path = os.getenv("DB_PATH")
    database_file = Path(db_path).expanduser() if db_path else BASE_DIR / "schedule.db"

    if not database_file.is_absolute():
        database_file = (BASE_DIR / database_file).resolve()

    # 确保父目录存在，避免 "unable to open database file"。
    database_file.parent.mkdir(parents=True, exist_ok=True)

    return f"sqlite:///{database_file.as_posix()}"


DATABASE_URL = resolve_database_url()
IS_SQLITE = DATABASE_URL.startswith("sqlite")

#: SQLite 数据库文件路径，非 SQLite 时为 None。
DATABASE_FILE: Optional[Path] = None
if IS_SQLITE:
    raw_path = DATABASE_URL.split("sqlite:///", 1)[-1]
    if raw_path and raw_path != ":memory:":
        DATABASE_FILE = Path(raw_path)


#: 连接池上限。SQLite 单文件写只能串行，池开太大只会在数据库层排队，
#: 因此默认 5 + 10 溢出，够 15 个并发请求；超过的请求在池上等待。
#: 这是一条真实存在的天花板：要多撑并发必须换 Postgres（改 DATABASE_URL 即可）。
POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "10"))
POOL_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT", "30"))

#: 内存库（测试）用 StaticPool，不接受 pool_size 参数，单独分支。
IS_MEMORY_SQLITE = IS_SQLITE and ":memory:" in DATABASE_URL

_engine_kwargs: dict = {
    # SQLite 默认禁止跨线程使用连接，FastAPI 的线程池需要关闭此限制。
    "connect_args": {"check_same_thread": False} if IS_SQLITE else {},
    "future": True,
}
if not IS_MEMORY_SQLITE:
    _engine_kwargs.update(
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT,
    )

engine = create_engine(DATABASE_URL, **_engine_kwargs)


def configure_sqlite_connection(dbapi_connection) -> None:
    """给单个 SQLite 连接设置外键、日志模式与锁等待。

    单独抽成函数是为了可测：监听器只在模块级 engine 上注册，而测试不该为了
    验证 pragma 去写真实数据库文件。

    这三条不是"调优"，是正确性：

    - ``foreign_keys``：SQLite 默认不校验外键，删任务会留下孤立日程。
    - ``journal_mode=WAL``：默认的 rollback journal 下，一个写事务会挡住所有
      读。本应用的写事务要跨多轮工具调用，锁窗口太长；WAL 让读写互不阻塞，
      写与写之间才需要排队。
    - ``busy_timeout``：遇到写锁时等多久再报 "database is locked"。sqlite3
      默认 5 秒，对一个可能同时有十几个请求提交的服务偏短。
    """

    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    # 内存库会返回 "memory"，设不上去，不算错误。
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=15000")
    cursor.close()


if IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        configure_sqlite_connection(dbapi_connection)


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    future=True,
)

Base = declarative_base()


def ensure_schema(target_engine) -> None:
    """为旧版 SQLite 数据库补齐新增列。

    ``Base.metadata.create_all`` 只会创建缺失的表，不会给已存在的表加列，
    因此这里用 PRAGMA 检查并按需 ALTER TABLE。
    """

    if not IS_SQLITE:
        return

    with target_engine.begin() as connection:
        columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(tasks)")
        }
        if columns and "start_time" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE tasks ADD COLUMN start_time DATETIME"
            )
        if columns and "location_id" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE tasks ADD COLUMN location_id INTEGER"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_tasks_location_id ON tasks (location_id)"
            )
        task_location_additions = {
            "location": "VARCHAR(200)",
            "location_city": "VARCHAR(100)",
            "location_longitude": "FLOAT",
            "location_latitude": "FLOAT",
            "splittable": "BOOLEAN NOT NULL DEFAULT 0",
            "min_chunk_minutes": "INTEGER NOT NULL DEFAULT 30",
            "max_chunk_minutes": "INTEGER NOT NULL DEFAULT 120",
        }
        for name, sql_type in task_location_additions.items():
            if columns and name not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE tasks ADD COLUMN {name} {sql_type}"
                )

        event_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(events)")
        }
        if event_columns and "location_id" not in event_columns:
            connection.exec_driver_sql(
                "ALTER TABLE events ADD COLUMN location_id INTEGER"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_events_location_id ON events (location_id)"
            )
        if event_columns and "event_type" not in event_columns:
            connection.exec_driver_sql(
                "ALTER TABLE events ADD COLUMN event_type VARCHAR(20) "
                "NOT NULL DEFAULT 'activity'"
            )
        if event_columns and "travel_mode" not in event_columns:
            connection.exec_driver_sql(
                "ALTER TABLE events ADD COLUMN travel_mode VARCHAR(20)"
            )
        migrate_legacy_trips = bool(event_columns and "trip_id" not in event_columns)
        event_location_additions = {
            "trip_id": "VARCHAR(32)",
            "trip_title": "VARCHAR(200)",
            "trip_order": "INTEGER",
            "location_city": "VARCHAR(100)",
            "location_longitude": "FLOAT",
            "location_latitude": "FLOAT",
            "origin_location": "VARCHAR(200)",
            "origin_location_id": "INTEGER",
            "origin_location_city": "VARCHAR(100)",
            "origin_location_longitude": "FLOAT",
            "origin_location_latitude": "FLOAT",
        }
        for name, sql_type in event_location_additions.items():
            if event_columns and name not in event_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE events ADD COLUMN {name} {sql_type}"
                )
        if event_columns:
            walking_match = "title LIKE '%步行%'"
            taxi_match = "title LIKE '%打车%'"
            driving_match = "title LIKE '%驾车%'"
            if "description" in event_columns:
                walking_match += (
                    " OR (description LIKE '%步行%' AND description NOT LIKE '%公交%' "
                    "AND description NOT LIKE '%地铁%' AND description NOT LIKE '%打车%' "
                    "AND description NOT LIKE '%驾车%')"
                )
                taxi_match += (
                    " OR (description LIKE '%打车%' AND description NOT LIKE '%公交%' "
                    "AND description NOT LIKE '%地铁%' AND description NOT LIKE '%驾车%')"
                )
                driving_match += (
                    " OR (description LIKE '%驾车%' AND description NOT LIKE '%公交%' "
                    "AND description NOT LIKE '%地铁%' AND description NOT LIKE '%打车%')"
                )
            connection.exec_driver_sql(
                "UPDATE events SET travel_mode = 'walking' "
                "WHERE event_type = 'travel' AND travel_mode IS NULL "
                f"AND ({walking_match})"
            )
            connection.exec_driver_sql(
                "UPDATE events SET travel_mode = 'taxi' "
                "WHERE event_type = 'travel' AND travel_mode IS NULL "
                f"AND ({taxi_match})"
            )
            connection.exec_driver_sql(
                "UPDATE events SET travel_mode = 'driving' "
                "WHERE event_type = 'travel' AND travel_mode IS NULL "
                f"AND ({driving_match})"
            )
            connection.exec_driver_sql(
                "UPDATE events SET travel_mode = 'transit' "
                "WHERE event_type = 'travel' AND travel_mode IS NULL"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_events_origin_location_id "
                "ON events (origin_location_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_events_trip_id ON events (trip_id)"
            )
            if migrate_legacy_trips and "start_time" in event_columns:
                connection.exec_driver_sql(
                    "UPDATE events SET "
                    "trip_id = 'legacy-' || substr(start_time, 1, 10), "
                    "trip_title = '历史行程' "
                    "WHERE trip_id IS NULL AND substr(start_time, 1, 10) IN ("
                    "SELECT substr(start_time, 1, 10) FROM events "
                    "GROUP BY substr(start_time, 1, 10) HAVING COUNT(*) >= 3)"
                )
                connection.exec_driver_sql(
                    "UPDATE events SET trip_order = ("
                    "SELECT COUNT(*) FROM events earlier "
                    "WHERE earlier.trip_id = events.trip_id "
                    "AND (earlier.start_time < events.start_time "
                    "OR (earlier.start_time = events.start_time AND earlier.id <= events.id))) "
                    "WHERE trip_id IS NOT NULL AND trip_order IS NULL"
                )

        schedule_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(schedules)")
        }
        schedule_additions = {
            "plan_batch_id": "VARCHAR(32)",
            "reason": "TEXT",
            "updated_at": "DATETIME",
            "plan_version_id": "VARCHAR(32)",
            "is_locked": "BOOLEAN NOT NULL DEFAULT 0",
            "chunk_index": "INTEGER NOT NULL DEFAULT 1",
            "source": "VARCHAR(20) NOT NULL DEFAULT 'auto'",
        }
        for name, sql_type in schedule_additions.items():
            if schedule_columns and name not in schedule_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE schedules ADD COLUMN {name} {sql_type}"
                )
        if schedule_columns:
            connection.exec_driver_sql(
                "UPDATE schedules SET updated_at = created_at WHERE updated_at IS NULL"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_schedules_plan_batch_id "
                "ON schedules (plan_batch_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_schedules_plan_version_id "
                "ON schedules (plan_version_id)"
            )

        chat_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(chat_messages)")
        }
        if chat_columns and "session_id" not in chat_columns:
            connection.exec_driver_sql(
                "ALTER TABLE chat_messages ADD COLUMN session_id VARCHAR(64) DEFAULT 'legacy'"
            )
        if chat_columns:
            if "agent_run_id" not in chat_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE chat_messages ADD COLUMN agent_run_id VARCHAR(32)"
                )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_chat_messages_session_id "
                "ON chat_messages (session_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_chat_messages_agent_run_id "
                "ON chat_messages (agent_run_id)"
            )


def check_database() -> bool:
    """探测数据库是否真的可连接、可查询。"""

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def get_db():
    """FastAPI 依赖：提供一个请求级别的数据库会话。"""

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
