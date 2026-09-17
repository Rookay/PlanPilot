# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：agent.py
"""Agent 门面：组装 LLM / Registry / Memory / Confirmation / Skills，并提供统一入口。"""

from sqlalchemy.orm import Session

from app.agent.confirmation import PendingActionStore
from app.agent.loop import AgentLoop, AgentResult
from app.agent.memory import Memory
from app.llm.base import LLMProvider
from app.skills.base import SkillRegistry
from app.tools.registry import ToolRegistry


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
            return self._handle_confirmation(
                db, confirm_action_id, decision or "accept", session_id
            )
        return self.loop.run(db, message, session_id)

    def _handle_confirmation(
        self,
        db: Session,
        action_id: str,
        decision: str,
        session_id: str,
    ) -> AgentResult:
        pending = self.confirmation_store.get(action_id, session_id)
        if pending is None:
            result = AgentResult(
                reply="该确认请求不存在或已过期，请重新发起操作。",
                session_id=session_id,
            )
            self.memory.add(session_id, "assistant", result.reply)
            return result

        if decision == "reject":
            self.confirmation_store.remove(action_id)
            result = AgentResult(reply="已取消该操作。", session_id=session_id)
            self.memory.add(session_id, "assistant", result.reply)
            return result

        applied: list = []
        proposed: list = []
        summaries: list = []
        errors: list = []
        for call in pending.tool_calls:
            result = self.registry.execute(call["name"], call["args"], db)
            if result.success:
                summaries.append(result.summary or call["name"])
                applied.append({"name": call["name"], "summary": result.summary})
                if (
                    call["name"] in ("generate_plan", "replan")
                    and isinstance(result.data, dict)
                    and isinstance(result.data.get("schedules"), list)
                ):
                    proposed = result.data["schedules"]
            else:
                errors.append(result.error or f"{call['name']} 执行失败")

        self.confirmation_store.remove(action_id)

        if errors:
            reply = "操作未能完成：" + "；".join(errors)
        else:
            reply = "已执行：" + "；".join(summaries) if summaries else "操作完成。"

        result = AgentResult(
            reply=reply,
            session_id=session_id,
            applied_changes=applied,
            proposed_schedules=proposed,
        )
        self.memory.add(session_id, "assistant", result.reply)
        return result
