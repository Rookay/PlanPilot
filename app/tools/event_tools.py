# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：event_tools.py
"""Event 相关工具。"""

from typing import Any

from sqlalchemy.orm import Session

from app.integrations.maps.base import MapIntegrationError
from app.integrations.maps.service import get_map_service
from app.models.event import Event
from app.models.schedule import Schedule
from app.schemas.event import EventCreate, EventSeriesCreate, EventUpdate, TripRevision
from app.services import event_service
from app.services.travel_planning_service import (
    LocatedBlock,
    PlanningLocation,
    TravelConstraintChecker,
)
from app.tools.base import Tool, ToolResult
from app.tools.serializers import event_to_dict
from app.utils import parse_naive_utc


class GetEventsTool(Tool):
    name = "get_events"
    description = "查询固定行程（不可移动的外部约束，如会议、预约、车次），可限定时间范围。"
    input_schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "范围开始时间 ISO 8601 UTC"},
            "end": {"type": "string", "description": "范围结束时间 ISO 8601 UTC"},
        },
        "required": [],
        "additionalProperties": False,
    }
    output_schema = {"type": "array", "items": {"type": "object"}}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        events = event_service.list_events(
            db,
            start=parse_naive_utc(kwargs.get("start")),
            end=parse_naive_utc(kwargs.get("end")),
        )
        return ToolResult(
            success=True,
            data=[event_to_dict(e) for e in events],
            summary=f"共 {len(events)} 个事件",
        )


class GetEventTool(Tool):
    name = "get_event"
    description = "按 id 查询单个事件。"
    input_schema = {
        "type": "object",
        "properties": {"event_id": {"type": "integer"}},
        "required": ["event_id"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            event = event_service.get_event(db, kwargs["event_id"])
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(success=True, data=event_to_dict(event))


class GetTripsTool(Tool):
    name = "get_trips"
    description = "查询由多段事件组成的旅行行程组，用于后续整体顺延或局部调整。"
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    output_schema = {"type": "array", "items": {"type": "object"}}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        trips = event_service.list_trips(db)
        return ToolResult(
            success=True,
            data=[item.model_dump(mode="json") for item in trips],
            summary=f"共 {len(trips)} 个行程组",
        )


class CreateEventTool(Tool):
    name = "create_event"
    description = "创建时间已经确定、不可被规划引擎移动的固定行程，如会议、预约、车次。"
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "start_time": {"type": "string", "description": "ISO 8601"},
            "end_time": {"type": "string", "description": "ISO 8601"},
            "event_type": {
                "type": "string",
                "enum": ["activity", "travel"],
                "description": "普通活动用 activity；时间段本身用于从出发地赶往目的地时用 travel",
            },
            "travel_mode": {
                "type": "string",
                "enum": ["walking", "driving", "taxi", "transit"],
                "description": "赶路方式：步行 walking、自驾 driving、打车 taxi、公交/地铁 transit",
            },
            "description": {"type": "string"},
            "location": {"type": "string"},
            "location_id": {"type": "integer", "description": "常用地点 id，可选"},
            "location_city": {"type": "string"},
            "location_longitude": {"type": "number"},
            "location_latitude": {"type": "number"},
            "origin_location": {"type": "string", "description": "赶路事件的出发地名称"},
            "origin_location_id": {"type": "integer", "description": "赶路事件的出发地常用地点 id，可选"},
            "origin_location_city": {"type": "string"},
            "origin_location_longitude": {"type": "number"},
            "origin_location_latitude": {"type": "number"},
            "recurrence": {
                "type": "string",
                "enum": ["daily", "weekdays", "weekly"],
            },
            "occurrences": {"type": "integer", "minimum": 2, "maximum": 52},
        },
        "required": ["title", "start_time", "end_time"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "auto"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        payload = {
            "title": kwargs["title"],
            "start_time": parse_naive_utc(kwargs["start_time"]),
            "end_time": parse_naive_utc(kwargs["end_time"]),
            "event_type": kwargs.get("event_type", "activity"),
            "travel_mode": kwargs.get("travel_mode"),
            "description": kwargs.get("description"),
            "location": kwargs.get("location"),
            "location_id": kwargs.get("location_id"),
            "location_city": kwargs.get("location_city"),
            "location_longitude": kwargs.get("location_longitude"),
            "location_latitude": kwargs.get("location_latitude"),
            "origin_location": kwargs.get("origin_location"),
            "origin_location_id": kwargs.get("origin_location_id"),
            "origin_location_city": kwargs.get("origin_location_city"),
            "origin_location_longitude": kwargs.get("origin_location_longitude"),
            "origin_location_latitude": kwargs.get("origin_location_latitude"),
        }
        try:
            recurrence = kwargs.get("recurrence")
            if recurrence:
                series_payload = {
                    **payload,
                    "recurrence": recurrence,
                    "occurrences": kwargs.get("occurrences", 2),
                }
                events = event_service.create_event_series(
                    db, EventSeriesCreate(**series_payload)
                )
                return ToolResult(
                    success=True,
                    data=[event_to_dict(item) for item in events],
                    summary=f"已创建 {len(events)} 个重复事件",
                )
            event = event_service.create_event(db, EventCreate(**payload))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data=event_to_dict(event),
            summary=f"已创建事件「{event.title}」",
        )


class CreateEventsTool(Tool):
    name = "create_events"
    description = (
        "一次性创建一组按时间排列的固定行程，并在执行前让用户统一确认。"
        "明确日期的旅行、游玩、接站等多地点日程必须使用本工具；"
        "赶路段用 event_type=travel，并填写与路线查询一致的 travel_mode；"
        "游览、用餐、入住等用 activity。工具会在确认前复核每段路线时间。"
    )
    _event_properties = {
        "title": {"type": "string"},
        "start_time": {"type": "string", "description": "ISO 8601 UTC"},
        "end_time": {"type": "string", "description": "ISO 8601 UTC"},
        "event_type": {"type": "string", "enum": ["activity", "travel"]},
        "travel_mode": {
            "type": "string",
            "enum": ["walking", "driving", "taxi", "transit"],
            "description": "event_type=travel 时必填；打车填 taxi，但 estimate_travel_time 使用 driving",
        },
        "description": {"type": "string"},
        "location": {"type": "string"},
        "location_id": {"type": "integer"},
        "location_city": {"type": "string"},
        "location_longitude": {"type": "number"},
        "location_latitude": {"type": "number"},
        "origin_location": {"type": "string"},
        "origin_location_id": {"type": "integer"},
        "origin_location_city": {"type": "string"},
        "origin_location_longitude": {"type": "number"},
        "origin_location_latitude": {"type": "number"},
    }
    input_schema = {
        "type": "object",
        "properties": {
            "trip_title": {"type": "string", "description": "整组行程名称"},
            "events": {
                "type": "array",
                "minItems": 1,
                "maxItems": 30,
                "items": {
                    "type": "object",
                    "properties": _event_properties,
                    "required": ["title", "start_time", "end_time", "event_type"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["events"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "confirm"

    @staticmethod
    def _payloads(kwargs: dict) -> list[EventCreate]:
        payloads = []
        for raw in kwargs.get("events") or []:
            values = dict(raw)
            values["start_time"] = parse_naive_utc(values.get("start_time"))
            values["end_time"] = parse_naive_utc(values.get("end_time"))
            payloads.append(EventCreate(**values))
        if not payloads:
            raise ValueError("至少需要一个行程项")
        return payloads

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            payloads = self._payloads(kwargs)
            conflict = self._existing_conflict(db, payloads)
            if conflict:
                return ToolResult(success=False, error=conflict)
            route_issues = self._route_issues(payloads)
            if route_issues:
                return ToolResult(success=False, error="；".join(route_issues))
            events = event_service.create_events(
                db,
                payloads,
                trip_title=kwargs.get("trip_title"),
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data={"events": [event_to_dict(item) for item in events]},
            summary=f"已创建 {len(events)} 段行程",
        )

    def preview(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            payloads = sorted(self._payloads(kwargs), key=lambda item: item.start_time)
            for previous, current in zip(payloads, payloads[1:]):
                if current.start_time < previous.end_time:
                    return ToolResult(
                        success=False,
                        error=f"行程「{previous.title}」与「{current.title}」时间重叠",
                    )
            conflict = self._existing_conflict(db, payloads)
            if conflict:
                return ToolResult(success=False, error=conflict)
            route_issues = self._route_issues(payloads)
            if route_issues:
                return ToolResult(success=False, error="；".join(route_issues))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))

        preview = []
        for item in payloads:
            preview.append(
                {
                    **item.model_dump(
                        exclude={
                            "description",
                            "location_id",
                            "origin_location_id",
                        }
                    ),
                    "start_time": item.start_time.isoformat() + "Z",
                    "end_time": item.end_time.isoformat() + "Z",
                }
            )
        titles = "、".join(item.title for item in payloads)
        trip_title = kwargs.get("trip_title") or "一日行程"
        return ToolResult(
            success=True,
            needs_confirmation=True,
            data={"events": preview},
            summary=f"将创建行程组「{trip_title}」，包含 {len(payloads)} 段：{titles}",
        )

    @staticmethod
    def _existing_conflict(
        db: Session,
        payloads: list[EventCreate],
        exclude_event_ids: set[int] | None = None,
    ) -> str | None:
        if not payloads:
            return None
        range_start = min(item.start_time for item in payloads)
        range_end = max(item.end_time for item in payloads)
        event_query = db.query(Event).filter(
            Event.start_time < range_end,
            Event.end_time > range_start,
        )
        if exclude_event_ids:
            event_query = event_query.filter(Event.id.notin_(exclude_event_ids))
        existing = [
            (item.title, item.start_time, item.end_time)
            for item in event_query.all()
        ]
        existing.extend(
            (f"任务安排 #{item.id}", item.start_time, item.end_time)
            for item in db.query(Schedule)
            .filter(
                Schedule.status.in_(("accepted", "proposed")),
                Schedule.start_time < range_end,
                Schedule.end_time > range_start,
            )
            .all()
        )
        for candidate in payloads:
            for title, start, end in existing:
                if candidate.start_time < end and start < candidate.end_time:
                    return f"行程「{candidate.title}」与已有安排「{title}」时间冲突"
        return None

    @staticmethod
    def _route_issues(payloads: list[EventCreate]) -> list[str]:
        blocks = []
        for index, item in enumerate(payloads):
            if item.location_longitude is None or item.location_latitude is None:
                continue
            blocks.append(
                LocatedBlock(
                    key=f"candidate-event-{index}",
                    title=item.title,
                    start=item.start_time,
                    end=item.end_time,
                    location=PlanningLocation(
                        label=item.location or item.title,
                        city=item.location_city,
                        longitude=item.location_longitude,
                        latitude=item.location_latitude,
                    ),
                    travel=item.event_type == "travel",
                    travel_mode=item.travel_mode,
                )
            )
        if len(blocks) < 2:
            return []
        try:
            map_service = get_map_service()
        except MapIntegrationError:
            return []
        checker = TravelConstraintChecker(map_service, blocks, {})
        return checker.sequence_issues([])


class ReviseTripTool(Tool):
    name = "revise_trip"
    description = (
        "原子调整一个旅行行程组。可整体顺延/提前、批量修改事件、删除景点或赶路段、"
        "更换交通方式和重命名；执行前统一展示完整新时间线并确认。"
    )
    _update_properties = {
        "event_id": {"type": "integer"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "start_time": {"type": "string"},
        "end_time": {"type": "string"},
        "event_type": {"type": "string", "enum": ["activity", "travel"]},
        "travel_mode": {
            "type": "string",
            "enum": ["walking", "driving", "taxi", "transit"],
        },
        "location": {"type": "string"},
        "location_id": {"type": "integer"},
        "location_city": {"type": "string"},
        "location_longitude": {"type": "number"},
        "location_latitude": {"type": "number"},
        "origin_location": {"type": "string"},
        "origin_location_id": {"type": "integer"},
        "origin_location_city": {"type": "string"},
        "origin_location_longitude": {"type": "number"},
        "origin_location_latitude": {"type": "number"},
    }
    input_schema = {
        "type": "object",
        "properties": {
            "trip_id": {"type": "string"},
            "trip_title": {"type": "string"},
            "shift_minutes": {
                "type": "integer",
                "minimum": -1440,
                "maximum": 1440,
                "description": "整组平移分钟数，正数顺延、负数提前",
            },
            "updates": {
                "type": "array",
                "maxItems": 30,
                "items": {
                    "type": "object",
                    "properties": _update_properties,
                    "required": ["event_id"],
                    "additionalProperties": False,
                },
            },
            "delete_event_ids": {
                "type": "array",
                "maxItems": 30,
                "items": {"type": "integer"},
            },
        },
        "required": ["trip_id"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "confirm"

    @staticmethod
    def _revision(kwargs: dict) -> TripRevision:
        return TripRevision.model_validate(kwargs)

    def _validated_preview(self, db: Session, revision: TripRevision) -> dict:
        preview = event_service.preview_trip_revision(db, revision)
        payloads = [payload for _, payload in preview["after"]]
        conflict = CreateEventsTool._existing_conflict(
            db,
            payloads,
            exclude_event_ids={event.id for event in preview["before"]},
        )
        if conflict:
            raise ValueError(conflict)
        route_issues = CreateEventsTool._route_issues(payloads)
        if route_issues:
            raise ValueError("；".join(route_issues))
        return preview

    def preview(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            revision = self._revision(kwargs)
            result = self._validated_preview(db, revision)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        before_by_id = {event.id: event for event in result["before"]}
        events = []
        for order, (event_id, payload) in enumerate(result["after"], start=1):
            before = before_by_id[event_id]
            events.append(
                {
                    **payload.model_dump(mode="json"),
                    "id": event_id,
                    "trip_id": revision.trip_id,
                    "trip_order": order,
                    "change": "updated"
                    if payload.model_dump() != {
                        name: getattr(before, name) for name in EventCreate.model_fields
                    }
                    else "unchanged",
                }
            )
        return ToolResult(
            success=True,
            needs_confirmation=True,
            data={
                "trip_id": revision.trip_id,
                "trip_title": result["trip_title"],
                "events": events,
                "deleted_event_ids": result["deleted_ids"],
                "shift_minutes": result["shift_minutes"],
            },
            summary=(
                f"将整体调整行程组「{result['trip_title']}」："
                f"保留 {len(events)} 段，删除 {len(result['deleted_ids'])} 段"
            ),
        )

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            revision = self._revision(kwargs)
            self._validated_preview(db, revision)
            events = event_service.apply_trip_revision(db, revision)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data={"events": [event_to_dict(event) for event in events]},
            summary=f"已更新行程组「{events[0].trip_title}」，现有 {len(events)} 段",
        )


class UpdateEventTool(Tool):
    name = "update_event"
    description = "更新事件字段（时间/地点等）。"
    input_schema = {
        "type": "object",
        "properties": {
            "event_id": {"type": "integer"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "start_time": {"type": "string"},
            "end_time": {"type": "string"},
            "event_type": {"type": "string", "enum": ["activity", "travel"]},
            "travel_mode": {
                "type": "string",
                "enum": ["walking", "driving", "taxi", "transit"],
            },
            "location": {"type": "string"},
            "location_id": {"type": "integer", "description": "常用地点 id，可选"},
            "location_city": {"type": "string"},
            "location_longitude": {"type": "number"},
            "location_latitude": {"type": "number"},
            "origin_location": {"type": "string"},
            "origin_location_id": {"type": "integer", "description": "赶路事件的出发地常用地点 id，可选"},
            "origin_location_city": {"type": "string"},
            "origin_location_longitude": {"type": "number"},
            "origin_location_latitude": {"type": "number"},
        },
        "required": ["event_id"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "auto"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        event_id = kwargs.pop("event_id")

        update = dict(kwargs)
        for key in ("start_time", "end_time"):
            if key in update and update[key] is not None:
                update[key] = parse_naive_utc(update[key])

        try:
            event = event_service.update_event(db, event_id, EventUpdate(**update))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data=event_to_dict(event),
            summary=f"已更新事件「{event.title}」",
        )


class DeleteEventTool(Tool):
    name = "delete_event"
    description = "删除事件。"
    input_schema = {
        "type": "object",
        "properties": {"event_id": {"type": "integer"}},
        "required": ["event_id"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "confirm"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            result = event_service.delete_event(db, kwargs["event_id"])
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True, data=result, summary=f"已删除事件 #{result['id']}"
        )

    def preview(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            event = event_service.get_event(db, kwargs["event_id"])
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            needs_confirmation=True,
            summary=f"将删除事件「{event.title}」",
        )


class DeleteEventsTool(Tool):
    name = "delete_events"
    description = (
        "一次删除多个固定事件。用户要求清空行程、删除一组同名/相关事件时，"
        "必须优先使用此工具，只产生一次确认。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "event_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
                "uniqueItems": True,
                "description": "要一起删除的全部事件 id",
            }
        },
        "required": ["event_ids"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "confirm"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            result = event_service.delete_events(db, kwargs["event_ids"])
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        titles = "、".join(f"「{title}」" for title in result["titles"])
        return ToolResult(
            success=True,
            data=result,
            summary=f"已删除 {result['deleted']} 个事件：{titles}",
        )

    def preview(self, db: Session, **kwargs: Any) -> ToolResult:
        event_ids = list(dict.fromkeys(kwargs.get("event_ids") or []))
        try:
            events = [event_service.get_event(db, event_id) for event_id in event_ids]
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=str(exc))
        if not events:
            return ToolResult(success=False, error="没有选择要删除的事件")
        titles = "、".join(f"「{event.title}」" for event in events)
        return ToolResult(
            success=True,
            needs_confirmation=True,
            data={"events": [event_to_dict(event) for event in events]},
            summary=f"将一次删除 {len(events)} 个事件：{titles}",
        )
