# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：agent.py
"""Agent 门面：组装 LLM / Registry / Memory / Confirmation / Skills，并提供统一入口。"""

import time

from sqlalchemy.orm import Session

from app.agent.confirmation import PendingActionStore
from app.agent.loop import AgentLoop, AgentResult
from app.agent.memory import Memory
from app.agent.transaction import AgentDatabaseSnapshot
from app.llm.base import LLMProvider
from app.skills.base import SkillRegistry
from app.tools.registry import ToolRegistry
from app.services import schedule_service


class Agent:
    def __init__(
        self,
        llm: LLMProvider,
        registry: ToolRegistry,
        memory: Memory,
        confirmation_store: PendingActionStore,
        skill_registry: SkillRegistry,
    ):
        self.loop = AgentLoop(
            llm=llm,
            registry=registry,
            memory=memory,
            confirmation_store=confirmation_store,
            skill_registry=skill_registry,
        )
        self.registry = registry
        self.memory = memory
        self.confirmation_store = confirmation_store

    def handle(
        self,
        db: Session,
        message: str,
        session_id: str,
        confirm_action_id: str | None = None,
        decision: str | None = None,
    ) -> AgentResult:
        if confirm_action_id:
            result = self._handle_confirmation(
                db, confirm_action_id, decision or "accept", session_id
            )
        else:
            result = self.loop.run(db, message, session_id)
        return self._finalize_result(db, result)

    def _handle_confirmation(
        self,
        db: Session,
        action_id: str,
        decision: str,
        session_id: str,
    ) -> AgentResult:
        pending = self.confirmation_store.get(db, action_id, session_id)
        if pending is None:
            result = AgentResult(
                reply="该确认请求不存在或已过期，请重新发起操作。",
                session_id=session_id,
            )
            self.memory.add(session_id, "assistant", result.reply)
            return result

        if decision == "reject":
            self.confirmation_store.remove(db, action_id)
            result = AgentResult(reply="已取消该操作。", session_id=session_id)
            self.memory.add(session_id, "assistant", result.reply)
            return result

        snapshot = AgentDatabaseSnapshot.capture(db)
        applied: list = []
        proposed: list = []
        summaries: list = []
        errors: list = []
        tool_calls: list = []
        for call in pending.tool_calls:
            started = time.perf_counter()
            result = self.registry.execute(call["name"], call["args"], db)
            duration_ms = int((time.perf_counter() - started) * 1000)
            tool_calls.append(
                {
                    "stage": self.loop._tool_stage(call["name"]),
                    "name": call["name"],
                    "arguments": call["args"],
                    "success": result.success,
                    "summary": result.summary or result.error,
                    "duration_ms": duration_ms,
                }
            )
            if result.success:
                summaries.append(result.summary or call["name"])
                applied.append(
                    {
                        "name": call["name"],
                        "summary": result.summary,
                        "data": result.data,
                    }
                )
                if (
                    call["name"] in ("generate_plan", "replan", "replan_local")
                    and isinstance(result.data, dict)
                    and isinstance(result.data.get("schedules"), list)
                ):
                    proposed = result.data["schedules"]
            else:
                errors.append(result.error or f"{call['name']} 执行失败")
                break

        self.confirmation_store.remove(db, action_id)

        if errors:
            snapshot.restore(db)
            applied = []
            proposed = []
            reply = "操作未能完成，已恢复修改前状态：" + "；".join(errors)
        else:
            reply = "已执行：" + "；".join(summaries) if summaries else "操作完成。"

        result = AgentResult(
            reply=reply,
            session_id=session_id,
            applied_changes=applied,
            proposed_schedules=proposed,
            tool_calls=tool_calls,
        )
        self.memory.add(session_id, "assistant", result.reply)
        return result

    def _finalize_result(self, db: Session, result: AgentResult) -> AgentResult:
        reply_before_audit = result.reply
        result.workflow_steps = [
            {
                "kind": "tool",
                "stage": item.get("stage") or self.loop._tool_stage(item.get("name", "")),
                "name": item.get("name"),
                "label": self.loop.tool_label(item.get("name", "")),
                "status": (
                    "waiting"
                    if item.get("awaiting_confirmation") and result.confirmation.required
                    else "warning"
                    if item.get("limited")
                    else "completed"
                    if item.get("success")
                    else "failed"
                ),
                "summary": item.get("summary"),
                "duration_ms": item.get("duration_ms", 0),
            }
            for item in result.tool_calls
        ]
        if result.applied_changes and not result.confirmation.required:
            audit = schedule_service.audit_applied_changes(db, result.applied_changes)
            if audit["audited"]:
                result.workflow_steps.append(
                    {
                        "kind": "audit",
                        "stage": "复查",
                        "name": "postflight_audit",
                        "label": self.loop.tool_label("postflight_audit"),
                        "status": "completed" if audit["valid"] else "warning",
                        "summary": "未发现冲突" if audit["valid"] else "；".join(audit["issues"]),
                    }
                )
                if audit["issues"]:
                    result.reply += " 复查发现：" + "；".join(audit["issues"])
        if result.reply != reply_before_audit:
            self.memory.replace_last_assistant(result.session_id, result.reply)
        return result
