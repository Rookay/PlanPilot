# -*- coding: UTF-8 -*-
"""Agent 执行轨迹的记录与查询。"""

import json
import uuid

from sqlalchemy.orm import Session

from app.models.agent_run import AgentRun


def record_run(
    db: Session,
    *,
    session_id: str,
    message: str,
    provider: str | None,
    skill: str | None,
    status: str,
    duration_ms: int,
    confirmation_required: bool,
    steps: list[dict],
    model_usage: dict | None = None,
    estimated_cost: float = 0,
) -> AgentRun:
    usage = model_usage or {}
    run = AgentRun(
        id=uuid.uuid4().hex,
        session_id=session_id,
        message=message or None,
        provider=provider,
        skill=skill,
        status=status,
        duration_ms=duration_ms,
        tool_count=sum(1 for item in steps if item.get("kind") == "tool"),
        confirmation_required=confirmation_required,
        constraint_passed=not any(
            item.get("status") in ("failed", "warning")
            for item in steps
            if item.get("stage") in ("验证", "复查")
        ),
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        estimated_cost=max(0, estimated_cost),
        steps_json=json.dumps(steps, ensure_ascii=False, default=str),
    )
    db.add(run)
    db.flush()
    return run


def list_runs(db: Session, session_id: str, limit: int = 20) -> list[dict]:
    rows = (
        db.query(AgentRun)
        .filter(AgentRun.session_id == session_id)
        .order_by(AgentRun.created_at.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )
    return [run_to_dict(item) for item in rows]


def clear_runs(db: Session, session_id: str) -> int:
    return (
        db.query(AgentRun)
        .filter(AgentRun.session_id == session_id)
        .delete(synchronize_session=False)
    )


def run_to_dict(run: AgentRun) -> dict:
    return {
        "id": run.id,
        "session_id": run.session_id,
        "message": run.message,
        "provider": run.provider,
        "skill": run.skill,
        "status": run.status,
        "duration_ms": run.duration_ms,
        "tool_count": run.tool_count,
        "confirmation_required": run.confirmation_required,
        "constraint_passed": run.constraint_passed,
        "model_usage": {
            "prompt_tokens": run.prompt_tokens,
            "completion_tokens": run.completion_tokens,
            "total_tokens": run.total_tokens,
        },
        "estimated_cost": run.estimated_cost,
        "steps": json.loads(run.steps_json or "[]"),
        "created_at": run.created_at,
    }
