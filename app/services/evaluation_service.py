# -*- coding: UTF-8 -*-
"""可重复运行的 Agent 端到端回归评测。"""

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401
from app.agent.agent import Agent
from app.agent.confirmation import PendingActionStore
from app.agent.memory import Memory
from app.database import Base
from app.llm.mock import MockProvider
from app.models.evaluation import EvaluationRun
from app.schemas.event import EventCreate
from app.schemas.task import TaskCreate
from app.services import event_service, schedule_service, task_service
from app.skills.planner_skills import build_default_skills
from app.tools.defaults import build_default_registry


@dataclass(frozen=True)
class EvaluationScenario:
    id: str
    name: str
    description: str
    expected_tools: tuple[str, ...]
    forbidden_tools: tuple[str, ...] = ()
    expects_confirmation: bool = False


SCENARIOS = (
    EvaluationScenario(
        id="urgent_task",
        name="紧急任务自动规划",
        description="识别高优先级任务并生成确定性排期",
        expected_tools=("get_tasks", "update_task", "generate_plan"),
    ),
    EvaluationScenario(
        id="trip_creation",
        name="旅行行程整组创建",
        description="旅行不得误用普通任务排期，并需要统一确认",
        expected_tools=("create_events",),
        forbidden_tools=("generate_plan",),
        expects_confirmation=True,
    ),
    EvaluationScenario(
        id="trip_revision",
        name="旅行行程整体顺延",
        description="整组事件必须原子平移并保留同一行程组",
        expected_tools=("revise_trip",),
        forbidden_tools=("update_event",),
        expects_confirmation=True,
    ),
    EvaluationScenario(
        id="batch_delete",
        name="相关事件批量删除",
        description="多个相关事件只产生一次确认并原子删除",
        expected_tools=("delete_events",),
        forbidden_tools=("delete_event",),
        expects_confirmation=True,
    ),
    EvaluationScenario(
        id="conflict_check",
        name="冲突查询",
        description="只读请求应调用冲突检查且不要求确认",
        expected_tools=("check_conflicts",),
    ),
)


def list_scenarios() -> list[dict]:
    return [
        {
            "id": item.id,
            "name": item.name,
            "description": item.description,
            "expected_tools": list(item.expected_tools),
            "forbidden_tools": list(item.forbidden_tools),
            "expects_confirmation": item.expects_confirmation,
        }
        for item in SCENARIOS
    ]


def run_evaluation(db: Session) -> dict:
    results = [_run_scenario(item) for item in SCENARIOS]
    count = len(results)
    passed = sum(1 for item in results if item["passed"])
    metrics = {
        "tool_selection_accuracy": _percentage(
            sum(1 for item in results if item["selection_passed"]), count
        ),
        "tool_success_rate": _percentage(
            sum(item["successful_tool_calls"] for item in results),
            sum(item["tool_calls"] for item in results),
        ),
        "constraint_pass_rate": _percentage(
            sum(1 for item in results if item["constraints_passed"]), count
        ),
        "confirmation_safety_rate": _percentage(
            sum(1 for item in results if item["confirmation_passed"]), count
        ),
        "average_latency_ms": round(
            sum(item["duration_ms"] for item in results) / max(1, count)
        ),
        "total_model_tokens": sum(item["model_tokens"] for item in results),
        "estimated_model_cost": round(
            sum(item["estimated_cost"] for item in results), 6
        ),
    }
    score = round(
        metrics["tool_selection_accuracy"] * 0.35
        + metrics["tool_success_rate"] * 0.25
        + metrics["constraint_pass_rate"] * 0.25
        + metrics["confirmation_safety_rate"] * 0.15,
        1,
    )
    run = EvaluationRun(
        id=uuid.uuid4().hex,
        mode="deterministic",
        score=score,
        scenario_count=count,
        passed_count=passed,
        metrics_json=json.dumps(metrics, ensure_ascii=False),
        results_json=json.dumps(results, ensure_ascii=False),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return _run_to_dict(run)


def list_runs(db: Session, limit: int = 20) -> list[dict]:
    rows = (
        db.query(EvaluationRun)
        .order_by(EvaluationRun.created_at.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )
    return [_run_to_dict(item) for item in rows]


def latest_run(db: Session) -> dict | None:
    row = db.query(EvaluationRun).order_by(EvaluationRun.created_at.desc()).first()
    return _run_to_dict(row) if row is not None else None


def _run_scenario(scenario: EvaluationScenario) -> dict:
    session = _isolated_session()
    started = time.perf_counter()
    state_check: Callable[[], bool] = lambda: True
    try:
        provider, prompt, state_check = _prepare_scenario(session, scenario.id)
        agent = Agent(
            llm=provider,
            registry=build_default_registry(),
            memory=Memory(),
            confirmation_store=PendingActionStore(),
            skill_registry=build_default_skills(),
        )
        first = agent.handle(session, prompt, f"eval-{scenario.id}")
        model_tokens = int(first.model_usage.get("total_tokens") or 0)
        confirmation_passed = first.confirmation.required == scenario.expects_confirmation
        calls = list(first.tool_calls)
        if first.confirmation.required:
            completed = agent.handle(
                session,
                "",
                f"eval-{scenario.id}",
                confirm_action_id=first.confirmation.action_id,
                decision="accept",
            )
            calls.extend(completed.tool_calls)
            model_tokens += int(completed.model_usage.get("total_tokens") or 0)
        names = [item.get("name") for item in calls]
        selection_passed = all(name in names for name in scenario.expected_tools) and not any(
            name in names for name in scenario.forbidden_tools
        )
        successful = sum(1 for item in calls if item.get("success"))
        audit = schedule_service.audit_current_plan(session)
        constraints_passed = audit["valid"] and state_check()
        error = None
    except Exception as exc:  # noqa: BLE001
        names = []
        calls = []
        successful = 0
        selection_passed = False
        confirmation_passed = False
        constraints_passed = False
        error = str(exc)
        model_tokens = 0
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        session.close()
    passed = selection_passed and confirmation_passed and constraints_passed and not error
    return {
        "id": scenario.id,
        "name": scenario.name,
        "passed": passed,
        "selection_passed": selection_passed,
        "confirmation_passed": confirmation_passed,
        "constraints_passed": constraints_passed,
        "tool_calls": len(calls),
        "successful_tool_calls": successful,
        "actual_tools": names,
        "expected_tools": list(scenario.expected_tools),
        "duration_ms": duration_ms,
        "model_tokens": model_tokens,
        "estimated_cost": 0,
        "error": error,
    }


def _prepare_scenario(db: Session, scenario_id: str):
    if scenario_id == "urgent_task":
        task = task_service.create_task(
            db,
            TaskCreate(title="论文实验", priority="medium", estimated_duration=60),
        )
        return (
            MockProvider(),
            "论文比较急，尽快安排，其余的安排到后面，留出空闲",
            lambda: task_service.get_task(db, task.id).priority == "high",
        )

    if scenario_id == "trip_creation":
        events = _sample_trip_payloads()
        provider = MockProvider(
            script=[
                {
                    "tool_calls": [
                        {
                            "name": "create_events",
                            "arguments": {"trip_title": "马鞍山第一天", "events": events},
                        }
                    ]
                }
            ]
        )
        return (
            provider,
            "安排马鞍山第一天行程",
            lambda: len({item.trip_id for item in event_service.list_events(db)}) == 1,
        )

    if scenario_id == "trip_revision":
        events = event_service.create_events(
            db,
            [EventCreate.model_validate(item) for item in _sample_trip_payloads()],
            trip_title="马鞍山第一天",
        )
        original = {item.id: item.start_time for item in events}
        trip_id = events[0].trip_id
        provider = MockProvider(
            script=[
                {
                    "tool_calls": [
                        {
                            "name": "revise_trip",
                            "arguments": {"trip_id": trip_id, "shift_minutes": 60},
                        }
                    ]
                }
            ]
        )
        return (
            provider,
            "把这一天整体顺延一小时",
            lambda: all(
                int((item.start_time - original[item.id]).total_seconds() // 60) == 60
                for item in event_service.get_trip_events(db, trip_id)
            ),
        )

    if scenario_id == "batch_delete":
        events = [
            event_service.create_event(
                db,
                EventCreate(
                    title=title,
                    start_time=datetime(2026, 10, 1 + index, 2),
                    end_time=datetime(2026, 10, 1 + index, 3),
                ),
            )
            for index, title in enumerate(("出去玩", "上海行程第1天", "上海行程第2天"))
        ]
        provider = MockProvider(
            script=[
                {
                    "tool_calls": [
                        {
                            "name": "delete_events",
                            "arguments": {"event_ids": [item.id for item in events]},
                        }
                    ]
                }
            ]
        )
        return provider, "清空上海行程和出去玩", lambda: not event_service.list_events(db)

    return MockProvider(), "检查一下当前安排有没有冲突", lambda: True


def _sample_trip_payloads() -> list[dict]:
    return [
        {
            "title": "抵达马鞍山东站",
            "start_time": "2026-09-27T02:00:00Z",
            "end_time": "2026-09-27T02:10:00Z",
            "event_type": "activity",
            "location": "马鞍山东站",
        },
        {
            "title": "打车前往酒店",
            "start_time": "2026-09-27T02:10:00Z",
            "end_time": "2026-09-27T02:40:00Z",
            "event_type": "travel",
            "travel_mode": "taxi",
            "origin_location": "马鞍山东站",
            "location": "酒店",
        },
        {
            "title": "酒店寄存行李",
            "start_time": "2026-09-27T02:40:00Z",
            "end_time": "2026-09-27T03:00:00Z",
            "event_type": "activity",
            "location": "酒店",
        },
    ]


def _isolated_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _percentage(numerator: int, denominator: int) -> float:
    return round(numerator * 100 / max(1, denominator), 1)


def _run_to_dict(run: EvaluationRun) -> dict:
    return {
        "id": run.id,
        "mode": run.mode,
        "score": run.score,
        "scenario_count": run.scenario_count,
        "passed_count": run.passed_count,
        "metrics": json.loads(run.metrics_json or "{}"),
        "results": json.loads(run.results_json or "[]"),
        "created_at": run.created_at,
    }
