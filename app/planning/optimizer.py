# -*- coding: UTF-8 -*-
"""Optional CP-SAT based global planner.

The module stays importable when OR-Tools is not installed. The service layer can
therefore fall back to the deterministic greedy planner without breaking startup.
"""

from datetime import datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.planning.engine import (
    SLOT_MINUTES,
    PlanAssignment,
    PlanFailure,
    PlanResult,
    PlanTask,
)

try:
    from ortools.sat.python import cp_model
except ImportError:  # pragma: no cover - availability depends on deployment extras
    cp_model = None


def is_cp_sat_available() -> bool:
    return cp_model is not None


def generate_optimized_plan(
    *,
    start: datetime,
    tasks: Sequence[PlanTask],
    busy_intervals: Iterable[Tuple[datetime, datetime]],
    scheduled_task_ids: Set[int],
    completed_task_ids: Optional[Set[int]],
    dependency_completion_times: Optional[Dict[int, datetime]],
    windows: List[Tuple[datetime, datetime]],
    gap_minutes: int,
    strategy: str,
    candidate_validator: Optional[
        Callable[[PlanTask, datetime, datetime, Sequence[PlanAssignment]], bool]
    ] = None,
    time_limit_seconds: float = 5.0,
) -> PlanResult:
    """Optimize whole-task placement over 15-minute candidate slots."""

    if cp_model is None:
        raise RuntimeError("当前环境未安装 OR-Tools")
    if any(task.splittable for task in tasks):
        raise RuntimeError("深度优化暂不支持可拆分任务")

    completed = set(scheduled_task_ids) | set(completed_task_ids or set())
    completion_times = dict(dependency_completion_times or {})
    pending = [task for task in tasks if task.id not in completed]
    task_by_id = {task.id: task for task in pending}
    failures: List[PlanFailure] = []
    eligible: List[PlanTask] = []

    for task in pending:
        missing = [
            dep for dep in task.dependency_ids
            if dep not in completed and dep not in task_by_id
        ]
        if missing:
            failures.append(
                PlanFailure(task.id, task.title, "前置任务尚未完成：" + ", ".join(map(str, missing)))
            )
        elif task.deadline is not None and task.deadline <= start:
            failures.append(PlanFailure(task.id, task.title, "截止时间早于规划起始时间"))
        else:
            eligible.append(task)

    if not eligible:
        return PlanResult(failures=failures)

    origin = min(window[0] for window in windows)
    busy = list(busy_intervals)
    banned: set[Tuple[int, datetime]] = set()

    for _ in range(13):
        result = _solve_once(
            origin=origin,
            tasks=eligible,
            busy=busy,
            windows=windows,
            completed=completed,
            completion_times=completion_times,
            gap_minutes=gap_minutes,
            strategy=strategy,
            banned=banned,
            time_limit_seconds=time_limit_seconds,
        )
        invalid = _invalid_assignments(result.assignments, eligible, candidate_validator)
        if not invalid:
            result.failures = failures + result.failures
            return result
        for assignment in invalid:
            banned.add((assignment.task_id, assignment.start_time))

    result.failures = failures + result.failures
    return result


def _solve_once(
    *,
    origin: datetime,
    tasks: Sequence[PlanTask],
    busy: List[Tuple[datetime, datetime]],
    windows: List[Tuple[datetime, datetime]],
    completed: Set[int],
    completion_times: Dict[int, datetime],
    gap_minutes: int,
    strategy: str,
    banned: set[Tuple[int, datetime]],
    time_limit_seconds: float,
) -> PlanResult:
    model = cp_model.CpModel()
    horizon_ticks = max(_ticks(origin, end) for _, end in windows) + 1
    intervals = []
    choices: Dict[int, list] = {}
    scheduled_vars = {}

    for index, (busy_start, busy_end) in enumerate(busy):
        clipped_start = max(0, _floor_ticks(origin, busy_start))
        clipped_end = min(horizon_ticks, _ceil_ticks(origin, busy_end))
        if clipped_end > clipped_start:
            intervals.append(model.NewFixedSizeIntervalVar(clipped_start, clipped_end - clipped_start, f"busy_{index}"))

    for task in tasks:
        task_choices = []
        duration_ticks = max(1, _ceil_minutes(task.estimated_duration))
        occupancy_ticks = duration_ticks + _ceil_minutes(max(0, gap_minutes))
        earliest = task.start_time or task.earliest_start or origin
        dependency_ready = [completion_times[item] for item in task.dependency_ids if item in completion_times]
        if dependency_ready:
            earliest = max(earliest, max(dependency_ready))

        for window_start, window_end in windows:
            candidate = _ceil_moment(max(window_start, earliest))
            latest = window_end - timedelta(minutes=duration_ticks * SLOT_MINUTES)
            while candidate <= latest:
                if task.deadline is not None and candidate + timedelta(minutes=duration_ticks * SLOT_MINUTES) > task.deadline:
                    break
                if (task.id, candidate) not in banned:
                    choice = model.NewBoolVar(f"task_{task.id}_{_ticks(origin, candidate)}")
                    start_tick = _ticks(origin, candidate)
                    interval = model.NewOptionalFixedSizeIntervalVar(
                        start_tick,
                        occupancy_ticks,
                        choice,
                        f"slot_{task.id}_{start_tick}",
                    )
                    intervals.append(interval)
                    task_choices.append((choice, candidate, duration_ticks))
                candidate += timedelta(minutes=SLOT_MINUTES)

        scheduled = model.NewBoolVar(f"scheduled_{task.id}")
        scheduled_vars[task.id] = scheduled
        if task_choices:
            model.Add(sum(item[0] for item in task_choices) == scheduled)
        else:
            model.Add(scheduled == 0)
        choices[task.id] = task_choices

    model.AddNoOverlap(intervals)
    task_ids = set(choices)
    for task in tasks:
        for dependency_id in task.dependency_ids:
            if dependency_id in completed or dependency_id not in task_ids:
                continue
            model.Add(scheduled_vars[task.id] <= scheduled_vars[dependency_id])
            for child_var, child_start, _ in choices[task.id]:
                for parent_var, parent_start, parent_duration in choices[dependency_id]:
                    parent_end = parent_start + timedelta(minutes=parent_duration * SLOT_MINUTES)
                    if child_start < parent_end:
                        model.Add(child_var + parent_var <= 1)

    objective = []
    priority_score = {"high": 900, "medium": 600, "low": 350}
    for task in tasks:
        for choice, candidate, duration_ticks in choices[task.id]:
            score = priority_score.get(task.priority, 600) + duration_ticks * 20
            if strategy == "deadline_first" and task.deadline is not None:
                score += 500
            elif strategy == "priority_first":
                score += priority_score.get(task.priority, 600) // 2
            if any(start <= candidate and candidate + timedelta(minutes=duration_ticks * SLOT_MINUTES) <= end for start, end in task.preferred_windows):
                score += 180
            score -= min(_ticks(origin, candidate), 10000) // 8
            objective.append(choice * score)
    model.Maximize(sum(objective))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 1
    status = solver.Solve(model)
    assignments: List[PlanAssignment] = []
    selected_ids = set()
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for task in tasks:
            for choice, candidate, duration_ticks in choices[task.id]:
                if solver.Value(choice):
                    reason = _reason(task, candidate, duration_ticks)
                    assignments.append(
                        PlanAssignment(
                            task.id,
                            candidate,
                            candidate + timedelta(minutes=duration_ticks * SLOT_MINUTES),
                            reason,
                        )
                    )
                    selected_ids.add(task.id)

    assignments.sort(key=lambda item: (item.start_time, item.task_id))
    unscheduled = [
        PlanFailure(task.id, task.title, "在给定范围内无法同时满足全部约束")
        for task in tasks
        if task.id not in selected_ids
    ]
    return PlanResult(assignments=assignments, failures=unscheduled)


def _invalid_assignments(assignments, tasks, validator):
    if validator is None:
        return []
    task_by_id = {task.id: task for task in tasks}
    invalid = []
    for assignment in assignments:
        others = [item for item in assignments if item is not assignment]
        if not validator(task_by_id[assignment.task_id], assignment.start_time, assignment.end_time, others):
            invalid.append(assignment)
    return invalid


def _reason(task: PlanTask, start: datetime, duration_ticks: int) -> str:
    labels = {"high": "高优先级", "medium": "中优先级", "low": "低优先级"}
    parts = [labels.get(task.priority, "中优先级"), "全局优化"]
    if task.deadline is not None:
        parts.append("已考虑截止时间")
    end = start + timedelta(minutes=duration_ticks * SLOT_MINUTES)
    if any(window_start <= start and end <= window_end for window_start, window_end in task.preferred_windows):
        parts.append("符合偏好时段")
    return " · ".join(parts)


def _ceil_moment(moment: datetime) -> datetime:
    value = moment.replace(second=0, microsecond=0)
    remainder = value.minute % SLOT_MINUTES
    return value + timedelta(minutes=SLOT_MINUTES - remainder) if remainder else value


def _ticks(origin: datetime, moment: datetime) -> int:
    return max(0, int((moment - origin).total_seconds() // (SLOT_MINUTES * 60)))


def _floor_ticks(origin: datetime, moment: datetime) -> int:
    return int((moment - origin).total_seconds() // (SLOT_MINUTES * 60))


def _ceil_ticks(origin: datetime, moment: datetime) -> int:
    seconds = (moment - origin).total_seconds()
    return int((seconds + SLOT_MINUTES * 60 - 1) // (SLOT_MINUTES * 60))


def _ceil_minutes(minutes: int) -> int:
    return max(0, (minutes + SLOT_MINUTES - 1) // SLOT_MINUTES)
