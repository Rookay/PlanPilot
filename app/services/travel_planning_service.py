# -*- coding: UTF-8 -*-
"""用地图路线时长约束排期候选，避免生成无法按时到达的安排。"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from app.integrations.maps.base import MapIntegrationError
from app.integrations.maps.schemas import Coordinate
from app.planning.engine import PlanAssignment, PlanTask
from app.schemas.event import TravelMode as EventTravelMode


MAX_CONTINUOUS_GAP = timedelta(hours=12)


@dataclass(frozen=True)
class PlanningLocation:
    label: str
    longitude: float
    latitude: float
    city: Optional[str] = None

    @property
    def coordinate(self) -> Coordinate:
        return Coordinate(longitude=self.longitude, latitude=self.latitude)

    @property
    def key(self) -> Tuple[float, float]:
        return (round(self.longitude, 6), round(self.latitude, 6))


@dataclass(frozen=True)
class LocatedBlock:
    key: str
    title: str
    start: datetime
    end: datetime
    location: PlanningLocation
    fixed: bool = True
    travel: bool = False
    travel_mode: Optional[EventTravelMode] = None


class TravelConstraintChecker:
    """缓存高德路线，并校验候选排期前后的可达性。"""

    def __init__(
        self,
        map_service,
        fixed_blocks: Iterable[LocatedBlock],
        task_locations: Dict[int, PlanningLocation],
    ):
        self.map_service = map_service
        self.fixed_blocks = list(fixed_blocks)
        self.task_locations = task_locations
        self.warnings: List[str] = []
        self.rejection_reasons: Dict[int, str] = {}
        self.validated_task_ids: set[int] = set()
        self._route_cache: Dict[Tuple, Optional[int]] = {}
        self._warning_set: set[str] = set()

    def validate_candidate(
        self,
        task: PlanTask,
        start: datetime,
        end: datetime,
        assignments: Sequence[PlanAssignment],
    ) -> bool:
        location = self.task_locations.get(task.id)
        if location is None:
            return True

        blocks = self.fixed_blocks + self.assignment_blocks(assignments)
        previous = self._previous_block(blocks, start)
        following = self._following_block(blocks, end)

        if previous is not None:
            required = self._travel_minutes(previous.location, location)
            available = _minutes_between(previous.end, start)
            if required is not None:
                self.validated_task_ids.add(task.id)
            if required is not None and required > available:
                self.rejection_reasons[task.id] = (
                    f"从「{previous.location.label}」前往「{location.label}」至少需要"
                    f" {required} 分钟，当前候选仅有 {available} 分钟"
                )
                return False

        if following is not None:
            required = self._travel_minutes(
                location,
                following.location,
                following.travel_mode or "transit",
            )
            departure = max(end, following.start) if following.travel else end
            deadline = following.end if following.travel else following.start
            available = _minutes_between(departure, deadline)
            if required is not None:
                self.validated_task_ids.add(task.id)
            if required is not None and required > available:
                self.rejection_reasons[task.id] = (
                    f"完成后前往「{following.location.label}」至少需要 {required} 分钟，"
                    f"当前候选仅有 {available} 分钟"
                )
                return False

        self.rejection_reasons.pop(task.id, None)
        return True

    def assignment_blocks(
        self, assignments: Sequence[PlanAssignment]
    ) -> List[LocatedBlock]:
        blocks: List[LocatedBlock] = []
        for assignment in assignments:
            location = self.task_locations.get(assignment.task_id)
            if location is not None:
                blocks.append(
                    LocatedBlock(
                        key=f"task-{assignment.task_id}",
                        title=f"任务 #{assignment.task_id}",
                        start=assignment.start_time,
                        end=assignment.end_time,
                        location=location,
                        fixed=False,
                    )
                )
        return blocks

    def sequence_issues(
        self,
        assignments: Sequence[PlanAssignment],
        focus_keys: Optional[set[str]] = None,
    ) -> List[str]:
        """复核最终顺序；传入 focus_keys 时只验证与本次变更相邻的路段。"""

        blocks = sorted(
            self.fixed_blocks + self.assignment_blocks(assignments),
            key=lambda item: (item.start, item.end, item.key),
        )
        issues: List[str] = []
        for previous, following in zip(blocks, blocks[1:]):
            if focus_keys is not None and not (
                previous.key in focus_keys or following.key in focus_keys
            ):
                continue
            departure = (
                max(previous.end, following.start)
                if following.travel
                else previous.end
            )
            deadline = following.end if following.travel else following.start
            gap = deadline - departure
            if gap < timedelta(0) or gap > MAX_CONTINUOUS_GAP:
                continue
            required = self._travel_minutes(
                previous.location,
                following.location,
                (following.travel_mode or "transit")
                if following.travel
                else "transit",
            )
            available = _minutes_between(departure, deadline)
            if required is None or required <= available:
                continue
            late = required - available
            kind = "固定行程冲突" if previous.fixed and following.fixed else "通勤冲突"
            issues.append(
                f"{kind}：从「{previous.location.label}」前往「{following.location.label}」"
                f"需要约 {required} 分钟，按当前时间会迟到 {late} 分钟"
            )
        return list(dict.fromkeys(issues))

    def _travel_minutes(
        self,
        origin: PlanningLocation,
        destination: PlanningLocation,
        mode: EventTravelMode = "transit",
    ) -> Optional[int]:
        if origin.key == destination.key:
            return 0
        city = origin.city or destination.city
        key = (origin.key, destination.key, city, mode)
        if key in self._route_cache:
            return self._route_cache[key]
        if self.map_service is None:
            self._route_cache[key] = None
            self._warn_once("未配置高德地图，含地点安排尚未校验通勤时间")
            return None
        try:
            response = self.map_service.get_routes(
                origin.coordinate,
                destination.coordinate,
                "driving" if mode == "taxi" else mode,
                city,
            )
            durations = [route.duration_minutes for route in response.routes]
            duration = min(durations) if durations else None
            if duration is None:
                self._warn_once(
                    f"高德未返回「{origin.label}」到「{destination.label}」的公交路线"
                )
            self._route_cache[key] = duration
            return duration
        except (MapIntegrationError, ValueError) as exc:
            self._route_cache[key] = None
            self._warn_once(f"通勤校验暂时失败：{exc}")
            return None

    @staticmethod
    def _previous_block(
        blocks: Sequence[LocatedBlock], start: datetime
    ) -> Optional[LocatedBlock]:
        candidates = [
            block
            for block in blocks
            if block.end <= start and start - block.end <= MAX_CONTINUOUS_GAP
        ]
        return max(candidates, key=lambda item: item.end, default=None)

    @staticmethod
    def _following_block(
        blocks: Sequence[LocatedBlock], end: datetime
    ) -> Optional[LocatedBlock]:
        candidates = [
            block
            for block in blocks
            if block.start >= end
            and (block.end if block.travel else block.start) - end <= MAX_CONTINUOUS_GAP
        ]
        return min(candidates, key=lambda item: item.start, default=None)

    def _warn_once(self, message: str) -> None:
        if message not in self._warning_set:
            self._warning_set.add(message)
            self.warnings.append(message)


def _minutes_between(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() // 60))
