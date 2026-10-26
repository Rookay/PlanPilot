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
        # ⚠️ 契约：以下 for 循环里通过 registry.execute 调用的工具
        # **不得** 新开 Session——所有改动必须走同一个 Session，否则
        # ``AgentDatabaseSnapshot`` 的写集抓不到，失败时无法补偿。
        # service 层自己 commit 是允许的（见 transaction.py 顶部说明）。
        pending = self.confirmation_store.get(db, action_id, session_id)
        if pending is None:
            return AgentResult(
                reply="该确认请求不存在或已过期，请重新发起操作。",
                session_id=session_id,
            )

        if decision == "reject":
            self.confirmation_store.remove(db, action_id)
            return AgentResult(reply="已取消该操作。", session_id=session_id)

        snapshot = AgentDatabaseSnapshot.capture(db)
        applied: list = []
        proposed: list = []
        summaries: list = []
        errors: list = []
        tool_calls: list = []
        trajectory: list = []
        for index, call in enumerate(pending.tool_calls):
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
            trajectory.append(
                self._confirmation_trajectory_item(index, action_id, call, result)
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

        if errors:
            # 先回滚再清待确认：restore 会按写集撤销本次改动，失败时保留
            # pending 记录，用户可以重新发起确认再试一次。
            snapshot.restore(db)
            applied = []
            proposed = []
            reply = "操作未能完成，已恢复修改前状态：" + "；".join(errors)
        else:
            snapshot.release(db)
            self.confirmation_store.remove(db, action_id)
            reply = "已执行：" + "；".join(summaries) if summaries else "操作完成。"

        result = AgentResult(
            reply=reply,
            session_id=session_id,
            applied_changes=applied,
            proposed_schedules=proposed,
            tool_calls=tool_calls,
            trajectory=trajectory,
        )
        return result

    def _confirmation_trajectory_item(
        self,
        index: int,
        action_id: str,
        call: dict,
        result,
    ) -> dict:
        """为"确认后真正执行"的那次调用造一条轨迹记录。

        确认路径不经过 ``AgentLoop.run``，拿不到模型给的 ``tool_call_id``。
        这里用 ``confirm-{action_id}-{index}`` 合成：同一次确认内唯一，且能反查
        到是哪个待确认操作落地的。

        没有这段，轨迹里会只剩"等待确认"那条、真正的执行一片空白——正是用户
        最想知道"到底改了什么"的那一步。
        """

        raw = self.loop._as_text(
            result.data
            if result.data is not None
            else (result.summary or result.error or "")
        )
        limit = AgentLoop.TRAJECTORY_RESULT_CHARS
        return {
            "turn_index": index,
            "tool_call_id": f"confirm-{action_id}-{index}",
            "name": call["name"],
            "arguments": self.loop._as_text(call["args"]),
            "success": bool(result.success),
            "summary": result.summary,
            "error": None if result.success else (result.error or "执行失败"),
            "result": (
                raw if len(raw) <= limit else raw[:limit] + "…[轨迹落库时截断]"
            ),
            "result_chars": len(raw),
        }

    def _finalize_result(self, db: Session, result: AgentResult) -> AgentResult:
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
        # 复查补充的结论文本直接落在 result.reply 上：API 层写 chat_messages 时
        # 取的就是这个值，不需要再回头改某个已经存下来的副本。
        return result
