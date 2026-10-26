# -*- coding: UTF-8 -*-
"""方案对比的数据结构构造。

把"当前库里的状态"和"规划器给出的候选"折算成可逐项比较的
保留 / 移动 / 新增 / 移除四类，供预览、多方案对比和局部重排共用。
放在独立模块是为了打断 plans 与 alternatives 的循环引用。
"""

from itertools import zip_longest
from typing import Dict, Iterable, List

from app.models.schedule import Schedule
from app.planning.engine import (
    PlanAssignment,
)


def _schedule_dict(schedule: Schedule) -> dict:
    return {
        "id": schedule.id,
        "task_id": schedule.task_id,
        "start_time": schedule.start_time.isoformat() + "Z",
        "end_time": schedule.end_time.isoformat() + "Z",
        "status": schedule.status,
        "is_locked": schedule.is_locked,
        "chunk_index": schedule.chunk_index,
        "source": schedule.source,
        "reason": schedule.reason,
        "plan_version_id": schedule.plan_version_id,
    }


def _assignment_dict(assignment) -> dict:
    return {
        "task_id": assignment.task_id,
        "start_time": assignment.start_time.isoformat() + "Z",
        "end_time": assignment.end_time.isoformat() + "Z",
        "chunk_index": assignment.chunk_index,
        "reason": assignment.reason,
    }


def _build_plan_comparison(
    current: Iterable[Schedule],
    proposed: Iterable[PlanAssignment],
    *,
    preserved_count: int = 0,
) -> dict:
    """按任务和分段顺序对齐旧、新方案，生成可解释的差异。"""

    current_by_task: Dict[int, List[Schedule]] = {}
    for item in current:
        current_by_task.setdefault(item.task_id, []).append(item)
    proposed_by_task: Dict[int, List[PlanAssignment]] = {}
    for item in proposed:
        proposed_by_task.setdefault(item.task_id, []).append(item)

    changes = []
    counts = {"added": 0, "removed": 0, "moved": 0, "unchanged": 0}
    for task_id in sorted(set(current_by_task) | set(proposed_by_task)):
        before_items = sorted(
            current_by_task.get(task_id, []),
            key=lambda item: (item.start_time, item.end_time, item.id),
        )
        after_items = sorted(
            proposed_by_task.get(task_id, []),
            key=lambda item: (item.start_time, item.end_time, item.chunk_index),
        )
        for before, after in zip_longest(before_items, after_items):
            before_data = _schedule_dict(before) if before is not None else None
            after_data = _assignment_dict(after) if after is not None else None
            if before is None:
                change_type = "added"
            elif after is None:
                change_type = "removed"
            elif (
                before.start_time == after.start_time
                and before.end_time == after.end_time
            ):
                change_type = "unchanged"
            else:
                change_type = "moved"
            counts[change_type] += 1
            changes.append(
                {
                    "type": change_type,
                    "task_id": task_id,
                    "before": before_data,
                    "after": after_data,
                    "reason": after.reason if after is not None else "当前范围不再适合该安排",
                }
            )

    return {
        "counts": {**counts, "preserved": preserved_count},
        "changes": changes,
    }
