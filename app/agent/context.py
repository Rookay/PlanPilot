# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：context.py
"""Context Builder：构造发送给 LLM 的上下文。

原则：不把整个数据库发给 LLM，只给一个紧凑快照，其余按需通过工具查询。
"""

from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.schedule import Schedule
from app.models.task import Task
from app.utils import local_now, utc_offset_hours, utcnow

_SYSTEM_PROMPT = """你是 PlanPilot 的个人规划助手，帮助用户管理任务（Task）、固定事件（Event）和时间安排（Schedule）。

核心原则：
1. 你不直接计算排期，也不直接读写数据库；一切操作必须通过提供的工具完成。
2. 排期、冲突检测、空闲时间等确定性计算，必须调用 generate_plan / check_conflicts / find_free_slots 等工具，不要自己心算。
3. 生成日程会产生 proposed（待确认）状态，这是非破坏性的，可直接执行；删除任务/事件/日程等破坏性操作需要用户确认。
4. 同一个工具和同一组参数不要重复调用；一旦 generate_plan / replan / check_conflicts 等工具返回成功，必须用自然语言总结结果，不要继续循环调用工具。
5. 用简体中文、简洁地回复，说明你做了什么以及结果。

时区规则（非常重要）：
- 系统内部统一使用 UTC 存储时间，传给工具的时间必须是 UTC（ISO 8601，带 Z 后缀）。
- 用户所在时区是 {timezone}（UTC{offset_sign}{offset_abs}）。
- 用户说的「上午/下午/晚上/几点」都是本地时间，你必须先按用户时区理解，再转换为 UTC 传给工具。
- 例：用户在 UTC+8，说「下午 2 点」，应传 06:00Z。
"""


def build_system_prompt(timezone: str = "Asia/Shanghai") -> str:
    offset = utc_offset_hours(timezone)
    offset_sign = "+" if offset >= 0 else "-"
    offset_abs = abs(offset)
    return _SYSTEM_PROMPT.format(
        timezone=timezone,
        offset_sign=offset_sign,
        offset_abs=offset_abs,
    )


def build_world_summary(db: Session, timezone: str = "Asia/Shanghai", limit: int = 20) -> str:
    """构建当前世界的紧凑快照。"""

    now = utcnow()
    local = local_now(timezone)
    offset = utc_offset_hours(timezone)

    tasks = (
        db.query(Task)
        .filter(Task.status.in_(["pending", "in_progress"]))
        .order_by(Task.created_at.desc())
        .limit(limit)
        .all()
    )
    events = db.query(Event).order_by(Event.start_time).limit(limit).all()
    schedules = db.query(Schedule).order_by(Schedule.start_time).limit(limit).all()

    lines = [
        f"当前时间：UTC {now.isoformat()} | 本地({timezone}) {local.isoformat()}（UTC{offset:+d}）"
    ]

    lines.append("\n待办任务：")
    if tasks:
        for task in tasks:
            deadline = task.deadline.isoformat() if task.deadline else "无"
            lines.append(
                f"- #{task.id} {task.title} | 时长 {task.estimated_duration} 分 | "
                f"优先级 {task.priority} | 截止 {deadline}"
            )
    else:
        lines.append("- （无）")

    lines.append("\n固定事件：")
    if events:
        for event in events:
            lines.append(
                f"- #{event.id} {event.title} | {event.start_time} ~ {event.end_time}"
            )
    else:
        lines.append("- （无）")

    lines.append("\n已有日程：")
    if schedules:
        for schedule in schedules:
            lines.append(
                f"- #{schedule.id} 任务#{schedule.task_id} | "
                f"{schedule.start_time} ~ {schedule.end_time} | {schedule.status}"
            )
    else:
        lines.append("- （无）")

    return "\n".join(lines)
