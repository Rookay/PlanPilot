from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models  # noqa: F401


def make_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    # autoflush 必须和生产 SessionLocal 一致（app/database.py 里是 False）。
    # 测试用默认的 True 会掩盖"同一事务内查不到刚 add 的记录"这类 bug——
    # memory_facts 的唯一约束冲突就是这么漏出去的。
    return sessionmaker(bind=engine, autoflush=False, future=True)()
