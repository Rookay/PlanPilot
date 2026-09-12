# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：main.py
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from app.api import agent, chat, events, schedules, tasks
from app.database import (
    DATABASE_FILE,
    DATABASE_URL,
    Base,
    check_database,
    engine,
    ensure_schema,
)

# 导入模型，确保 SQLAlchemy 能注册所有表。
from app import models  # noqa: F401

logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if DATABASE_FILE is not None:
        logger.info("数据库连接串: sqlite:///%s", DATABASE_FILE)
        logger.info("数据库文件存在: %s", "是" if DATABASE_FILE.exists() else "否（将自动创建）")
    else:
        logger.info("数据库连接串: %s", DATABASE_URL)

    try:
        Base.metadata.create_all(bind=engine)
        ensure_schema(engine)
    except Exception:
        logger.exception(
            "数据库初始化失败：请检查数据库路径与写权限（可用环境变量 DB_PATH 指定可写目录）"
        )
        raise

    logger.info("数据库初始化完成，连通性: %s", "正常" if check_database() else "异常")
    yield


app = FastAPI(
    title="Schedule Planner API",
    version="0.2.0",
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    # 这是一个本地/内网小工具，且没有使用 Cookie/凭证，直接放开所有来源，
    # 避免换主机、换端口、用 IP 访问时被浏览器拦截。
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(SQLAlchemyError)
async def _sqlalchemy_error_handler(_request: Request, exc: SQLAlchemyError):
    """把数据库异常转成可读的 503，方便前端直接看到原因。"""

    logger.exception("数据库操作失败: %s", exc)

    cause = getattr(exc, "orig", None) or exc
    return JSONResponse(
        status_code=503,
        content={"detail": f"数据库错误: {cause}"},
    )


app.include_router(tasks.router)
app.include_router(events.router)
app.include_router(schedules.router)
app.include_router(chat.router)
app.include_router(agent.router)

# 把前端目录作为静态文件托管：打开 http://127.0.0.1:8000/frontend/ 即可使用。
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/frontend", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")


@app.get("/")
def root():
    return {
        "name": "Schedule Planner API",
        "version": "0.2.0",
        "database": DATABASE_URL,
        "frontend": "/frontend/",
    }


@app.get("/health")
def health():
    ok = check_database()
    return {
        "status": "ok" if ok else "degraded",
        "database": "ok" if ok else "error",
    }
