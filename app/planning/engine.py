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
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# 时间粒度（分钟）：所有自动生成的日程都会对齐到该粒度。
SLOT_MINUTES = 15

# 默认允许安排任务的时间段。
DEFAULT_DAY_START = time(9, 0)
DEFAULT_DAY_END = time(22, 0)

# 优先级排序权重，数值越小越先安排。
PRIORITY_WEIGHT = {"high": 0, "medium": 1, "low": 2}
PRIORITY_SCORE = {"high": 900, "medium": 600, "low": 350}


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
    preferred_windows: List[Tuple[datetime, datetime]] = field(default_factory=list)
    splittable: bool = False
    min_chunk_minutes: int = 30
    max_chunk_minutes: int = 120
    dependency_ids: List[int] = field(default_factory=list)


@dataclass
class PlanAssignment:
    """一条排期结果。"""

    task_id: int
    start_time: datetime
    end_time: datetime
    reason: str = ""
    chunk_index: int = 1


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
    candidate_is_valid: Optional[Callable[[datetime, datetime], bool]] = None,
) -> Optional[datetime]:
    """在窗口内寻找第一个满足时间与附加约束的空闲时段。"""

    for window_start, window_end in windows:
        cursor = _ceil_to_slot(max(window_start, earliest))
        while cursor + duration <= window_end:
            slot_end = cursor + duration
            if deadline is not None and slot_end > deadline:
                # 窗口按时间递增，后面的候选只会更晚。
                return None

            overlapping_end = next(
                (
                    busy_end
                    for busy_start, busy_end in busy
                    if busy_end > cursor and busy_start < slot_end
                ),
                None,
            )
            if overlapping_end is not None:
                cursor = _ceil_to_slot(overlapping_end)
                continue

            if candidate_is_valid is None or candidate_is_valid(cursor, slot_end):
                return cursor
            cursor += timedelta(minutes=SLOT_MINUTES)

    return None


def planning_weight(
    task: PlanTask,
    strategy: str,
    reference_time: datetime,
) -> int:
    """Return a comparable task value for greedy and CP-SAT planning."""

    score = PRIORITY_SCORE.get(task.priority, 600)
    if strategy == "deadline_first" and task.deadline is not None:
        slack_hours = max(
            0,
            int((task.deadline - reference_time).total_seconds() // 3600),
        )
        # A deadline only dominates priority when it is genuinely close. Distant
        # deadlines retain a small tie-break bonus instead of a blanket boost.
        score += max(50, 1200 - slack_hours * 10)
    elif strategy == "priority_first":
        score += PRIORITY_SCORE.get(task.priority, 600) // 2
    return score


def _sort_key(
    task: PlanTask,
    strategy: str = "balanced",
    reference_time: Optional[datetime] = None,
):
    """按固定时间、用户选择的策略和创建时间稳定排序。"""

    start = task.start_time or task.earliest_start
    deadline = task.deadline
    fixed = (start is None, start or datetime.max)
    due = (deadline is None, deadline or datetime.max)
    reference = reference_time or task.created_at or datetime.min
    order = (*fixed, -planning_weight(task, strategy, reference), *due)
    return (*order, task.created_at or datetime.min)


def _intersect_windows(
    first: Sequence[Tuple[datetime, datetime]],
    second: Sequence[Tuple[datetime, datetime]],
) -> List[Tuple[datetime, datetime]]:
    intersections: List[Tuple[datetime, datetime]] = []
    for first_start, first_end in first:
        for second_start, second_end in second:
            start, end = max(first_start, second_start), min(first_end, second_end)
            if end > start:
                intersections.append((start, end))
    return sorted(intersections)


def generate_plan(
    *,
    start: datetime,
    days: int,
    tasks: Sequence[PlanTask],
    busy_intervals: Iterable[Tuple[datetime, datetime]],
    scheduled_task_ids: Set[int],
    completed_task_ids: Optional[Set[int]] = None,
    dependency_completion_times: Optional[Dict[int, datetime]] = None,
    day_start: time = DEFAULT_DAY_START,
    day_end: time = DEFAULT_DAY_END,
    windows: Optional[List[Tuple[datetime, datetime]]] = None,
    gap_minutes: int = 0,
    strategy: str = "balanced",
    candidate_validator: Optional[
        Callable[[PlanTask, datetime, datetime, Sequence[PlanAssignment]], bool]
    ] = None,
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
        windows: 显式指定可安排的时间窗口；传入后优先于 start/days/day_start/day_end。
        gap_minutes: 相邻任务之间的最小空闲间隔（分钟）。
        candidate_validator: 可选的候选时段校验器，用于通勤等跨事项约束。

    Returns:
        PlanResult：成功安排的任务 + 未能安排的任务及原因。

    Raises:
        ValueError: 规划时间窗口为空。
    """

    if windows is not None:
        planning_windows = sorted(windows)
    else:
        planning_windows = _build_windows(start, days, day_start, day_end)

    if not planning_windows:
        raise ValueError("规划时间窗口为空")

    horizon_start = planning_windows[0][0]
    horizon_end = planning_windows[-1][1]

    # 只保留与规划窗口有交集的忙碌区间。
    busy = [
        (interval_start, interval_end)
        for interval_start, interval_end in busy_intervals
        if interval_end > horizon_start and interval_start < horizon_end
    ]
    busy = _merge_intervals(busy)

    gap = timedelta(minutes=max(gap_minutes, 0))
    ordered = _dependency_order(tasks, strategy, horizon_start)

    assignments: List[PlanAssignment] = []
    failures: List[PlanFailure] = []
    completed = set(scheduled_task_ids) | set(completed_task_ids or set())
    completion_times = dict(dependency_completion_times or {})

    for task in ordered:
        if task.id in completed:
            continue

        incomplete_dependencies = [
            dependency_id
            for dependency_id in task.dependency_ids
            if dependency_id not in completed
        ]
        if incomplete_dependencies:
            failures.append(
                PlanFailure(
                    task.id,
                    task.title,
                    "前置任务尚未完成：" + ", ".join(map(str, incomplete_dependencies)),
                )
            )
            continue

        earliest = task.start_time or task.earliest_start or horizon_start
        dependency_times = [
            completion_times[item]
            for item in task.dependency_ids
            if item in completion_times
        ]
        if dependency_times:
            earliest = max(earliest, max(dependency_times))

        if task.deadline is not None and task.deadline <= start:
            failures.append(
                PlanFailure(task.id, task.title, "截止时间早于规划起始时间")
            )
            continue

        preferred_windows = _intersect_windows(planning_windows, task.preferred_windows)
        validate_candidate = (
            None
            if candidate_validator is None
            else lambda slot_start, slot_end: candidate_validator(
                task, slot_start, slot_end, assignments
            )
        )
        remaining = max(task.estimated_duration or 60, 1)
        chunk_index = 1
        while remaining > 0:
            chunk_candidates = _chunk_candidates(task, remaining)
            selected = None
            used_preference = False
            for chunk_minutes in chunk_candidates:
                duration = timedelta(minutes=chunk_minutes)
                slot_start = None
                if preferred_windows:
                    slot_start = _find_free_slot(
                        windows=preferred_windows,
                        busy=busy,
                        duration=duration,
                        earliest=max(earliest, horizon_start),
                        deadline=task.deadline,
                        candidate_is_valid=validate_candidate,
                    )
                    used_preference = slot_start is not None
                slot_start = slot_start or _find_free_slot(
                    windows=planning_windows,
                    busy=busy,
                    duration=duration,
                    earliest=max(earliest, horizon_start),
                    deadline=task.deadline,
                    candidate_is_valid=validate_candidate,
                )
                if slot_start is not None:
                    selected = (slot_start, duration, chunk_minutes)
                    break

            if selected is None:
                detail = (
                    f"，仍有 {remaining} 分钟未安排"
                    if task.splittable and chunk_index > 1
                    else ""
                )
                failures.append(
                    PlanFailure(
                        task.id,
                        task.title,
                        "在给定时间范围内找不到足够长的空闲时段" + detail,
                    )
                )
                break

            slot_start, duration, chunk_minutes = selected
            priority_labels = {"high": "高优先级", "medium": "中优先级", "low": "低优先级"}
            reason_parts = [priority_labels.get(task.priority, "中优先级")]
            if task.deadline is not None:
                reason_parts.append("已考虑截止时间")
            if used_preference:
                reason_parts.append("符合偏好时段")
            if task.splittable and task.estimated_duration > chunk_minutes:
                reason_parts.append(f"任务分段 {chunk_index}")
            assignment = PlanAssignment(
                task.id,
                slot_start,
                slot_start + duration,
                " · ".join(reason_parts),
                chunk_index=chunk_index,
            )
            assignments.append(assignment)
            busy = _merge_intervals(
                busy + [(slot_start - gap, slot_start + duration + gap)]
            )
            remaining -= chunk_minutes
            chunk_index += 1
            if not task.splittable:
                break

        if remaining <= 0:
            completed.add(task.id)
            completion_times[task.id] = max(
                item.end_time for item in assignments if item.task_id == task.id
            )

    return PlanResult(assignments=assignments, failures=failures)


def _dependency_order(
    tasks: Sequence[PlanTask],
    strategy: str,
    reference_time: datetime,
) -> List[PlanTask]:
    """在原有优先级排序上加入稳定的拓扑顺序。"""

    base = sorted(
        tasks,
        key=lambda task: _sort_key(task, strategy, reference_time),
    )
    by_id = {task.id: task for task in base}
    ordered: List[PlanTask] = []
    visiting: Set[int] = set()
    visited: Set[int] = set()

    def visit(task: PlanTask) -> None:
        if task.id in visited:
            return
        if task.id in visiting:
            return
        visiting.add(task.id)
        for dependency_id in task.dependency_ids:
            dependency = by_id.get(dependency_id)
            if dependency is not None:
                visit(dependency)
        visiting.remove(task.id)
        visited.add(task.id)
        ordered.append(task)

    for task in base:
        visit(task)
    return ordered


def _chunk_candidates(task: PlanTask, remaining: int) -> List[int]:
    if not task.splittable:
        return [remaining]
    maximum = min(max(task.max_chunk_minutes, 15), remaining)
    minimum = min(max(task.min_chunk_minutes, 15), remaining)
    candidates = list(range(maximum, minimum - 1, -SLOT_MINUTES))
    if minimum not in candidates:
        candidates.append(minimum)
    return list(dict.fromkeys(candidates))


# ---------------------------------------------------------------------------
# 分析类工具（确定性）
# ---------------------------------------------------------------------------


@dataclass
class Interval:
    """一个时间区间（用于冲突检测）。"""

    id: int
    title: str
    start: datetime
    end: datetime


@dataclass
class Conflict:
    """两个区间之间的重叠。"""

    first: Interval
    second: Interval
    overlap_minutes: int


@dataclass
class Evaluation:
    """计划评估结果。"""

    score: int  # 0-100
    issues: List[str]
    scheduled_count: int = 0
    unscheduled_count: int = 0
    scheduled_minutes: int = 0


def find_free_slots(
    *,
    start: datetime,
    days: int,
    busy_intervals: Iterable[Tuple[datetime, datetime]],
    day_start: time = DEFAULT_DAY_START,
    day_end: time = DEFAULT_DAY_END,
    min_duration: timedelta = timedelta(minutes=SLOT_MINUTES),
    windows: Optional[List[Tuple[datetime, datetime]]] = None,
) -> List[Tuple[datetime, datetime]]:
    """找出所有满足最短时长的空闲时间段（按时间升序）。"""

    planning_windows = sorted(windows) if windows is not None else _build_windows(
        start, days, day_start, day_end
    )
    if not planning_windows:
        return []

    horizon_start = planning_windows[0][0]
    horizon_end = planning_windows[-1][1]

    busy = [
        (interval_start, interval_end)
        for interval_start, interval_end in busy_intervals
        if interval_end > horizon_start and interval_start < horizon_end
    ]
    busy = _merge_intervals(busy)

    slots: List[Tuple[datetime, datetime]] = []

    for window_start, window_end in planning_windows:
        cursor = _ceil_to_slot(window_start)
        if cursor >= window_end:
            continue

        for busy_start, busy_end in busy:
            if busy_end <= cursor:
                continue
            if busy_start >= window_end:
                break
            if busy_start > cursor:
                gap_end = min(busy_start, window_end)
                if gap_end - cursor >= min_duration:
                    slots.append((cursor, gap_end))
            cursor = _ceil_to_slot(max(cursor, busy_end))
            if cursor >= window_end:
                break

        if cursor < window_end and (window_end - cursor) >= min_duration:
            slots.append((cursor, window_end))

    return slots


def check_conflicts(intervals: Sequence[Interval]) -> List[Conflict]:
    """检测时间区间之间的重叠（按开始时间排序后两两比较）。"""

    ordered = sorted(intervals, key=lambda item: item.start)
    conflicts: List[Conflict] = []

    for index, first in enumerate(ordered):
        for second in ordered[index + 1:]:
            if second.start >= first.end:
                break
            overlap = min(first.end, second.end) - second.start
            conflicts.append(
                Conflict(
                    first=first,
                    second=second,
                    overlap_minutes=int(overlap.total_seconds() // 60),
                )
            )

    return conflicts


def evaluate_plan(
    assignments: Sequence[PlanAssignment],
    failures: Sequence[PlanFailure],
    tasks_by_id: dict,
) -> Evaluation:
    """评估计划质量，返回 0-100 分与问题列表。"""

    issues: List[str] = []
    score = 100

    for failure in failures:
        issues.append(f"{failure.title}: {failure.reason}")
        score -= max(8, 40 // max(len(assignments) + len(failures), 1))

    for assignment in assignments:
        task = tasks_by_id.get(assignment.task_id)
        if (
            task is not None
            and task.deadline is not None
            and assignment.end_time > task.deadline
        ):
            issues.append(
                f"{task.title}: 结束于 {assignment.end_time}，晚于截止时间 {task.deadline}"
            )
            score -= 20

    score = max(0, min(100, score))
    scheduled_minutes = sum(
        int((item.end_time - item.start_time).total_seconds() // 60)
        for item in assignments
    )
    return Evaluation(
        score=score,
        issues=issues,
        scheduled_count=len(assignments),
        unscheduled_count=len(failures),
        scheduled_minutes=scheduled_minutes,
    )
