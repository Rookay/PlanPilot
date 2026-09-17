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


engine = create_engine(
    DATABASE_URL,
    # SQLite 默认禁止跨线程使用连接，FastAPI 的线程池需要关闭此限制。
    connect_args={"check_same_thread": False} if IS_SQLITE else {},
    future=True,
)


if IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        """每个连接开启外键约束，避免删除任务后残留孤立日程。"""

        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


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
        event_location_additions = {
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
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_events_origin_location_id "
                "ON events (origin_location_id)"
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
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_chat_messages_session_id "
                "ON chat_messages (session_id)"
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
