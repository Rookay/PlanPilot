# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：database.py
import logging
import os
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

# 项目根目录（app/ 的上一级）。
BASE_DIR = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)


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
    - ``journal_mode=WAL``：rollback journal 下写者提交时要拿独占锁，与读
      互相牵制；本应用的写事务要跨多轮工具调用，窗口长，牵连面就大。WAL 让
      读写真正并行，写与写之间才需要排队。注意它减少的是相互等待，不是消除
      写-写串行——实测同样的混合负载下快数倍，但并发上限仍由连接池决定。
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


#: 旧版本数据库需要补齐的列：``{表: {列名: 类型}}``。
#: 新增字段时只在这里加一行，不用再写一段 PRAGMA + if + ALTER。
#:
#: 类型用一份声明兼容 SQLite 与其他方言：SQLite 的 ALTER TABLE ADD COLUMN
#: 对类型很宽容，而 ``BOOLEAN NOT NULL DEFAULT 0`` 这类写法在 Postgres 上也成立。
_COLUMN_ADDITIONS: dict = {
    "tasks": {
        "start_time": "DATETIME",
        "location_id": "INTEGER",
        "location": "VARCHAR(200)",
        "location_city": "VARCHAR(100)",
        "location_longitude": "FLOAT",
        "location_latitude": "FLOAT",
        "splittable": "BOOLEAN NOT NULL DEFAULT 0",
        "min_chunk_minutes": "INTEGER NOT NULL DEFAULT 30",
        "max_chunk_minutes": "INTEGER NOT NULL DEFAULT 120",
    },
    "events": {
        "location_id": "INTEGER",
        "event_type": "VARCHAR(20) NOT NULL DEFAULT 'activity'",
        "travel_mode": "VARCHAR(20)",
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
    },
    "schedules": {
        "plan_batch_id": "VARCHAR(32)",
        "reason": "TEXT",
        "updated_at": "DATETIME",
        "plan_version_id": "VARCHAR(32)",
        "is_locked": "BOOLEAN NOT NULL DEFAULT 0",
        "chunk_index": "INTEGER NOT NULL DEFAULT 1",
        "source": "VARCHAR(20) NOT NULL DEFAULT 'auto'",
    },
    "chat_messages": {
        "session_id": "VARCHAR(64) DEFAULT 'legacy'",
        "agent_run_id": "VARCHAR(32)",
    },
    "memory_facts": {
        # 主题键：判断两条事实说的是不是同一件事，见 app/services/fact_service.py。
        "subject": "VARCHAR(120)",
    },
}

#: 需要在补列之后确保存在的索引：``(索引名, 表, 列)``。
_INDEXES: tuple = (
    ("ix_tasks_location_id", "tasks", "location_id"),
    ("ix_events_location_id", "events", "location_id"),
    ("ix_events_origin_location_id", "events", "origin_location_id"),
    ("ix_events_trip_id", "events", "trip_id"),
    ("ix_schedules_plan_batch_id", "schedules", "plan_batch_id"),
    ("ix_schedules_plan_version_id", "schedules", "plan_version_id"),
    ("ix_chat_messages_session_id", "chat_messages", "session_id"),
    ("ix_chat_messages_agent_run_id", "chat_messages", "agent_run_id"),
    ("ix_memory_facts_subject", "memory_facts", "subject"),
)


def ensure_schema(target_engine) -> None:
    """为旧版数据库补齐新增列与索引。

    ``Base.metadata.create_all`` 只会创建缺失的表，不会给已存在的表加列，
    因此这里做增量迁移。

    实现上是"声明 + 统一执行"：补哪些列写在 ``_COLUMN_ADDITIONS`` 里，执行
    只有一份逻辑。此前是 11 段手写 PRAGMA + if + ALTER 摊在 194 行里，每加
    一个字段就要再抄一遍——漏掉一次，旧库启动就报 no such column。

    列检查用 SQLAlchemy 的 inspector 而不是 ``PRAGMA table_info``，这样换
    Postgres 时结构迁移仍然有效（原实现在非 SQLite 下直接 return，换库即失去
    迁移能力）。只有依赖 SQLite 方言的数据回填才判断 ``IS_SQLITE``。
    """

    with target_engine.begin() as connection:
        inspector = inspect(connection)
        existing_tables = set(inspector.get_table_names())
        added: dict = {}

        for table, additions in _COLUMN_ADDITIONS.items():
            if table not in existing_tables:
                # 表还不存在：create_all 会用当前模型建出来，不需要补列。
                continue
            columns = {column["name"] for column in inspector.get_columns(table)}
            for name, sql_type in additions.items():
                if name in columns:
                    continue
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"
                )
                added.setdefault(table, set()).add(name)

        for index_name, table, column in _INDEXES:
            if table not in existing_tables:
                continue
            _create_index(connection, target_engine, index_name, table, column)

        _backfill(connection, existing_tables, added)


def _create_index(connection, target_engine, name: str, table: str, column: str) -> None:
    """建索引。SQLite 支持 IF NOT EXISTS，其他方言靠异常兜住重复创建。"""

    statement = f"CREATE INDEX {'IF NOT EXISTS ' if IS_SQLITE else ''}{name} ON {table} ({column})"
    try:
        connection.exec_driver_sql(statement)
    except Exception:  # noqa: BLE001 —— 索引已存在不是错误
        logger.debug("index %s already exists, skipped", name)


def _backfill(connection, existing_tables: set, added: dict) -> None:
    """给新补的列填初始值，并修一批历史数据。

    只在需要时执行：没有新列、也不是旧库，就不做全表 UPDATE。

    这里重新 ``inspect(connection)``：上面刚 ALTER 加过列，同一个 inspector
    拿到的是缓存里的旧列集合，会漏判。
    """

    inspector = inspect(connection)

    def columns_of(table: str) -> set:
        if table not in existing_tables:
            return set()
        return {column["name"] for column in inspector.get_columns(table)}

    event_columns = columns_of("events")
    if event_columns:
        _backfill_travel_mode(
            connection,
            event_columns,
            migrate_legacy_trips="trip_id" in added.get("events", set()),
        )

    schedules_columns = columns_of("schedules")
    if schedules_columns and "updated_at" in schedules_columns:
        connection.exec_driver_sql(
            "UPDATE schedules SET updated_at = created_at WHERE updated_at IS NULL"
        )


def _backfill_travel_mode(
    connection, event_columns: set, *, migrate_legacy_trips: bool
) -> None:
    """旧数据的交通方式推断，以及把一天多段行程归成一个 trip 组。

    只跑一次：``travel_mode`` 已是 'transit' 的行不会被再次覆盖；trip 归组只在
    ``trip_id`` 这一列刚刚补上时执行。
    """

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

    for mode, match in (
        ("walking", walking_match),
        ("taxi", taxi_match),
        ("driving", driving_match),
    ):
        connection.exec_driver_sql(
            f"UPDATE events SET travel_mode = '{mode}' "
            f"WHERE event_type = 'travel' AND travel_mode IS NULL AND ({match})"
        )
    connection.exec_driver_sql(
        "UPDATE events SET travel_mode = 'transit' "
        "WHERE event_type = 'travel' AND travel_mode IS NULL"
    )

    if not migrate_legacy_trips or "start_time" not in event_columns or not IS_SQLITE:
        # trip 归组依赖 substr 与 ||，是 SQLite 语法；换库时这段历史数据
        # 本身也不再存在，跳过即可。
        return

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
