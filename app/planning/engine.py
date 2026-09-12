# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：engine.py
"""确定性规划引擎。

职责：给定任务、忙碌区间和规划参数，计算出一组可执行的日程安排。

特点：
- 纯函数，不依赖数据库、不依赖 LLM；
- 幂等：同一输入必然得到同一输出；
- 所有时间均使用「不带时区的 UTC」（naive UTC），与项目其它模块一致。

这是 PlanPilot 中「可行性」的最终裁判，LLM 的推理不能绕过它。
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Iterable, List, Optional, Sequence, Set, Tuple

# 时间粒度（分钟）：所有自动生成的日程都会对齐到该粒度。
SLOT_MINUTES = 15

# 默认允许安排任务的时间段。
DEFAULT_DAY_START = time(9, 0)
DEFAULT_DAY_END = time(22, 0)

# 优先级排序权重，数值越小越先安排。
PRIORITY_WEIGHT = {"high": 0, "medium": 1, "low": 2}


@dataclass
class PlanTask:
    """规划引擎看到的任务视图（时间字段已归一化为 naive UTC）。"""

    id: int
    title: str
    estimated_duration: int = 60
    priority: str = "medium"
    start_time: Optional[datetime] = None
    earliest_start: Optional[datetime] = None
    deadline: Optional[datetime] = None
    created_at: Optional[datetime] = None


@dataclass
class PlanAssignment:
    """一条排期结果。"""

    task_id: int
    start_time: datetime
    end_time: datetime


@dataclass
class PlanFailure:
    """一个无法安排的任务及其原因。"""

    task_id: int
    title: str
    reason: str


@dataclass
class PlanResult:
    """一次规划的完整结果。"""

    assignments: List[PlanAssignment] = field(default_factory=list)
    failures: List[PlanFailure] = field(default_factory=list)


def _ceil_to_slot(moment: datetime) -> datetime:
    """把时间向上取整到下一个 SLOT_MINUTES 边界。"""

    moment = moment.replace(second=0, microsecond=0)
    remainder = moment.minute % SLOT_MINUTES
    if remainder:
        moment += timedelta(minutes=SLOT_MINUTES - remainder)
    return moment


def _merge_intervals(
    intervals: Iterable[Tuple[datetime, datetime]],
) -> List[Tuple[datetime, datetime]]:
    """合并重叠的时间区间，便于快速判断空闲时段。"""

    ordered = sorted(intervals, key=lambda item: item[0])
    if not ordered:
        return []

    merged = [ordered[0]]

    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    return merged


def _build_windows(
    start: datetime,
    days: int,
    day_start: time,
    day_end: time,
) -> List[Tuple[datetime, datetime]]:
    """生成每天的可工作窗口，并裁掉起始时间之前的部分。"""

    windows: List[Tuple[datetime, datetime]] = []
    first_day = start.date()

    for offset in range(days):
        day = first_day + timedelta(days=offset)
        window_start = max(datetime.combine(day, day_start), start)
        window_end = datetime.combine(day, day_end)
        if window_end > window_start:
            windows.append((window_start, window_end))

    return windows


def _find_free_slot(
    windows: List[Tuple[datetime, datetime]],
    busy: List[Tuple[datetime, datetime]],
    duration: timedelta,
    earliest: datetime,
    deadline: Optional[datetime],
) -> Optional[datetime]:
    """在窗口内寻找第一个足够长的空闲时段。"""

    for window_start, window_end in windows:
        cursor = _ceil_to_slot(max(window_start, earliest))
        if cursor >= window_end:
            continue

        for busy_start, busy_end in busy:
            if busy_end <= cursor:
                continue
            if busy_start >= window_end:
                break
            if cursor + duration <= busy_start:
                break
            cursor = _ceil_to_slot(max(cursor, busy_end))
            if cursor >= window_end:
                break

        if cursor + duration <= window_end:
            if deadline is not None and cursor + duration > deadline:
                # 窗口按时间递增，后面的窗口只会更晚，直接判定无法安排。
                return None
            return cursor

    return None


def _sort_key(task: PlanTask):
    """排序规则：先按开始时间，再按截止时间、优先级、创建时间。"""

    start = task.start_time or task.earliest_start
    deadline = task.deadline
    return (
        start is None,
        start or datetime.max,
        deadline is None,
        deadline or datetime.max,
        PRIORITY_WEIGHT.get(task.priority, 1),
        task.created_at or datetime.min,
    )


def generate_plan(
    *,
    start: datetime,
    days: int,
    tasks: Sequence[PlanTask],
    busy_intervals: Iterable[Tuple[datetime, datetime]],
    scheduled_task_ids: Set[int],
    day_start: time = DEFAULT_DAY_START,
    day_end: time = DEFAULT_DAY_END,
) -> PlanResult:
    """贪心排期。

    Args:
        start: 规划起始时间（naive UTC）。
        days: 规划天数。
        tasks: 待安排任务（时间已归一化）。
        busy_intervals: 已占用时间区间 ``(start, end)``（事件 + 已保留日程）。
        scheduled_task_ids: 已有日程的任务 id 集合，这些任务将被跳过。
        day_start: 每天可安排的最早时间。
        day_end: 每天可安排的最晚时间。

    Returns:
        PlanResult：成功安排的任务 + 未能安排的任务及原因。

    Raises:
        ValueError: 规划时间窗口为空。
    """

    windows = _build_windows(start, days, day_start, day_end)
    if not windows:
        raise ValueError("规划时间窗口为空")

    horizon_start = windows[0][0]
    horizon_end = windows[-1][1]

    # 只保留与规划窗口有交集的忙碌区间。
    busy = [
        (interval_start, interval_end)
        for interval_start, interval_end in busy_intervals
        if interval_end > horizon_start and interval_start < horizon_end
    ]
    busy = _merge_intervals(busy)

    ordered = sorted(tasks, key=_sort_key)

    assignments: List[PlanAssignment] = []
    failures: List[PlanFailure] = []

    for task in ordered:
        if task.id in scheduled_task_ids:
            continue

        duration = timedelta(minutes=max(task.estimated_duration or 60, 1))
        earliest = task.start_time or task.earliest_start or horizon_start

        if task.deadline is not None and task.deadline <= start:
            failures.append(
                PlanFailure(task.id, task.title, "截止时间早于规划起始时间")
            )
            continue

        slot_start = _find_free_slot(
            windows=windows,
            busy=busy,
            duration=duration,
            earliest=max(earliest, horizon_start),
            deadline=task.deadline,
        )

        if slot_start is None:
            failures.append(
                PlanFailure(
                    task.id,
                    task.title,
                    "在给定时间范围内找不到足够长的空闲时段",
                )
            )
            continue

        assignments.append(
            PlanAssignment(task.id, slot_start, slot_start + duration)
        )
        busy = _merge_intervals(busy + [(slot_start, slot_start + duration)])

    return PlanResult(assignments=assignments, failures=failures)
