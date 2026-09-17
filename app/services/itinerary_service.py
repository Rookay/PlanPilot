# -*- coding: UTF-8 -*-
"""按本地日期聚合事项、地点和通勤路线。"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.integrations.maps.base import MapIntegrationError
from app.integrations.maps.schemas import Coordinate, RouteOption, TravelMode
from app.integrations.maps.service import get_map_service
from app.models.event import Event
from app.models.location import SavedLocation
from app.models.schedule import Schedule
from app.models.task import Task
from app.schemas.itinerary import (
    DailyItineraryResponse,
    ItineraryItem,
    ItineraryLeg,
    ItineraryLocation,
    ItineraryMode,
)
from app.services import location_service, preference_service


def get_daily_itinerary(
    db: Session,
    target_date: date,
    mode: ItineraryMode = "smart",
    include_proposed: bool = True,
    include_return: bool = False,
    car_available: bool = False,
    parking_minutes: int = 10,
) -> DailyItineraryResponse:
    preferences = preference_service.get_preferences(db)
    day_start, day_end = _utc_day_bounds(target_date, preferences.timezone)
    locations = {item.id: item for item in location_service.list_locations(db)}

    items: List[ItineraryItem] = []
    events = (
        db.query(Event)
        .filter(Event.end_time > day_start, Event.start_time < day_end)
        .all()
    )
    for event in events:
        items.append(
            ItineraryItem(
                id=f"event-{event.id}",
                kind="event",
                source_id=event.id,
                title=event.title,
                start_time=event.start_time,
                end_time=event.end_time,
                status="fixed",
                event_type=event.event_type,
                origin_location=(
                    _location_model(locations.get(event.origin_location_id))
                    or _custom_location(
                        event.origin_location,
                        event.origin_location_city,
                        event.origin_location_longitude,
                        event.origin_location_latitude,
                    )
                ),
                location=(
                    _location_model(locations.get(event.location_id))
                    or _custom_location(
                        event.location,
                        event.location_city,
                        event.location_longitude,
                        event.location_latitude,
                    )
                ),
            )
        )

    statuses = ["accepted"]
    if include_proposed:
        statuses.append("proposed")
    scheduled = (
        db.query(Schedule, Task)
        .join(Task, Task.id == Schedule.task_id)
        .filter(
            Schedule.status.in_(statuses),
            Schedule.end_time > day_start,
            Schedule.start_time < day_end,
        )
        .all()
    )
    for schedule, task in scheduled:
        items.append(
            ItineraryItem(
                id=f"schedule-{schedule.id}",
                kind="task",
                source_id=task.id,
                schedule_id=schedule.id,
                title=task.title,
                start_time=schedule.start_time,
                end_time=schedule.end_time,
                status=schedule.status,
                location=(
                    _location_model(locations.get(task.location_id))
                    or _custom_location(
                        task.location,
                        task.location_city,
                        task.location_longitude,
                        task.location_latitude,
                    )
                ),
            )
        )
    items.sort(key=lambda item: (item.start_time, item.end_time, item.id))

    default = next((item for item in locations.values() if item.is_default), None)
    origin = _location_model(default)

    # 旧赶路事件没有独立出发地时，按时间线继承上一事项地点。
    previous_location = origin
    for item in items:
        if item.event_type == "travel" and item.origin_location is None:
            item.origin_location = previous_location
        if item.location is not None:
            previous_location = item.location

    map_service = None
    map_message = None
    try:
        map_service = get_map_service()
    except MapIntegrationError as exc:
        map_message = str(exc)

    requests: List[_LegRequest] = []
    previous_item: Optional[ItineraryItem] = None
    previous_location = origin
    for item in items:
        if item.location is None:
            previous_item = item
            continue

        if item.event_type == "travel":
            travel_origin = item.origin_location or previous_location
            if travel_origin is not None:
                requests.append(
                    _LegRequest(
                        origin_id=f"{item.id}:origin",
                        origin=travel_origin,
                        destination=item,
                        departure_base=item.start_time,
                    )
                )
        elif previous_location is not None:
            is_first_from_default = previous_item is None
            if not (
                is_first_from_default
                and previous_location.coordinate == item.location.coordinate
            ):
                requests.append(
                    _LegRequest(
                        origin_id=previous_item.id if previous_item else "origin",
                        origin=previous_location,
                        destination=item,
                        departure_base=(
                            previous_item.end_time if previous_item else None
                        ),
                    )
                )

        previous_location = item.location
        previous_item = item

    if (
        include_return
        and items
        and items[-1].location
        and origin
        and items[-1].location.coordinate != origin.coordinate
    ):
        last = items[-1]
        destination = ItineraryItem(
            id="origin-return",
            kind="event",
            source_id=origin.id,
            title=f"返回{origin.label}",
            start_time=last.end_time,
            end_time=last.end_time,
            status="return",
            location=origin,
        )
        requests.append(
            _LegRequest(
                origin_id=last.id,
                origin=last.location,
                destination=destination,
                departure_base=last.end_time,
                is_return=True,
            )
        )

    effective_car_available = car_available or mode == "driving"
    if mode == "smart":
        legs = _build_smart_legs(
            requests,
            map_service,
            effective_car_available,
            parking_minutes,
        )
    else:
        legs = [
            _build_leg(
                origin_id=request.origin_id,
                origin=request.origin,
                destination=request.destination,
                departure_base=request.departure_base,
                mode=mode,
                map_service=map_service,
                parking_minutes=(
                    parking_minutes
                    if mode == "driving" and not request.is_return
                    else 0
                ),
                recommendation_reason=(
                    f"已计入 {parking_minutes} 分钟停车预留"
                    if mode == "driving" and not request.is_return
                    else None
                ),
                car_location_after=(
                    request.destination.location.label if mode == "driving" else None
                ),
            )
            for request in requests
        ]

    for request, leg in zip(requests, legs):
        if request.is_return:
            leg.available_minutes = None
            leg.warning = None
    warnings = [leg.warning for leg in legs if leg.warning]

    return DailyItineraryResponse(
        date=target_date,
        timezone=preferences.timezone,
        mode=mode,
        include_return=include_return,
        car_available=effective_car_available,
        parking_minutes=parking_minutes,
        origin=origin,
        items=items,
        legs=legs,
        warnings=warnings,
        map_message=map_message,
    )


@dataclass(frozen=True)
class _LegRequest:
    origin_id: str
    origin: ItineraryLocation
    destination: ItineraryItem
    departure_base: Optional[datetime]
    is_return: bool = False


def _build_smart_legs(
    requests: List[_LegRequest],
    map_service,
    car_available: bool,
    parking_minutes: int,
) -> List[ItineraryLeg]:
    if not requests:
        return []

    modes: List[TravelMode] = ["walking", "transit"]
    if car_available:
        modes.append("driving")
    candidates = _load_route_candidates(requests, modes, map_service)

    initial_car = _coordinate_key(requests[0].origin) if car_available else None
    location_labels = {
        _coordinate_key(request.origin): request.origin.label for request in requests
    }
    location_labels.update(
        {
            _coordinate_key(request.destination.location): request.destination.location.label
            for request in requests
            if request.destination.location is not None
        }
    )
    locations_by_key = {
        _coordinate_key(request.origin): request.origin for request in requests
    }
    locations_by_key.update(
        {
            _coordinate_key(request.destination.location): request.destination.location
            for request in requests
            if request.destination.location is not None
        }
    )
    retrieval_cache: Dict[
        Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float]],
        Optional[Tuple[RouteOption, int]],
    ] = {}
    # car_position -> ((累计迟到分钟, 累计通勤分钟), 已选路线)
    states: Dict[
        Optional[Tuple[float, float]],
        Tuple[Tuple[int, int], List[ItineraryLeg]],
    ] = {initial_car: ((0, 0), [])}

    for index, request in enumerate(requests):
        assert request.destination.location is not None
        origin_key = _coordinate_key(request.origin)
        destination_key = _coordinate_key(request.destination.location)
        next_states: Dict[
            Optional[Tuple[float, float]],
            Tuple[Tuple[int, int], List[ItineraryLeg]],
        ] = {}

        for car_position, (previous_cost, previous_legs) in states.items():
            for candidate_mode in ("walking", "transit", "driving"):
                route = candidates.get((index, candidate_mode))
                retrieval_minutes = 0
                if candidate_mode == "driving":
                    if car_position is None:
                        continue
                    if car_position != origin_key:
                        cache_key = (origin_key, car_position, destination_key)
                        if cache_key not in retrieval_cache:
                            retrieval_cache[cache_key] = _route_via_parked_car(
                                map_service,
                                request.origin,
                                locations_by_key[car_position],
                                request.destination.location,
                            )
                        retrieved = retrieval_cache[cache_key]
                        if retrieved is None:
                            continue
                        route, retrieval_minutes = retrieved
                if route is None:
                    continue

                next_car_position = (
                    destination_key if candidate_mode == "driving" else car_position
                )
                parking = (
                    parking_minutes
                    if candidate_mode == "driving" and not request.is_return
                    else 0
                )
                reason = _smart_reason(
                    candidate_mode,
                    parking,
                    car_position,
                    origin_key,
                    destination_key,
                    request.origin.label,
                    retrieval_minutes,
                )
                leg = _build_leg(
                    origin_id=request.origin_id,
                    origin=request.origin,
                    destination=request.destination,
                    departure_base=request.departure_base,
                    mode=candidate_mode,
                    map_service=None,
                    route_option=route,
                    allow_lookup=False,
                    parking_minutes=parking,
                    recommendation_reason=reason,
                    car_location_after=location_labels.get(next_car_position),
                )
                late = 0 if request.is_return else _late_minutes(leg)
                cost = (
                    previous_cost[0] + late,
                    previous_cost[1] + (leg.duration_minutes or 0),
                )
                current = next_states.get(next_car_position)
                if current is None or cost < current[0]:
                    next_states[next_car_position] = (cost, previous_legs + [leg])

        if not next_states:
            # 地图暂时不可用时仍保留直线连线，不中断整个时间轴。
            for car_position, (previous_cost, previous_legs) in states.items():
                leg = _build_leg(
                    origin_id=request.origin_id,
                    origin=request.origin,
                    destination=request.destination,
                    departure_base=request.departure_base,
                    mode="transit",
                    map_service=None,
                    allow_lookup=False,
                    recommendation_reason="路线暂不可用，未完成智能推荐",
                    car_location_after=location_labels.get(car_position),
                )
                next_states[car_position] = (
                    (previous_cost[0], previous_cost[1] + 10_000),
                    previous_legs + [leg],
                )
        states = next_states

    final_location = _coordinate_key(requests[-1].destination.location)
    eligible_states = (
        [value for car_position, value in states.items() if car_position == final_location]
        if car_available
        else list(states.values())
    )
    return min(eligible_states or list(states.values()), key=lambda item: item[0])[1]


def _load_route_candidates(
    requests: List[_LegRequest],
    modes: List[TravelMode],
    map_service,
) -> Dict[Tuple[int, TravelMode], RouteOption]:
    if map_service is None:
        return {}

    results: Dict[Tuple[int, TravelMode], RouteOption] = {}
    jobs = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        for index, request in enumerate(requests):
            assert request.destination.location is not None
            for mode in modes:
                if request.origin.coordinate == request.destination.location.coordinate:
                    results[(index, mode)] = RouteOption(
                        distance_m=0,
                        duration_minutes=0,
                        mode=mode,
                        summary="同一地点",
                        path=[request.origin.coordinate],
                    )
                    continue
                future = executor.submit(
                    _fastest_route,
                    map_service,
                    request.origin,
                    request.destination.location,
                    mode,
                )
                jobs[future] = (index, mode)
        for future in as_completed(jobs):
            route = future.result()
            if route is not None:
                results[jobs[future]] = route
    return results


def _fastest_route(
    map_service,
    origin: ItineraryLocation,
    destination: ItineraryLocation,
    mode: TravelMode,
) -> Optional[RouteOption]:
    try:
        response = map_service.get_routes(
            origin.coordinate,
            destination.coordinate,
            mode,
            destination.city or origin.city,
        )
        return min(
            response.routes,
            key=lambda item: item.duration_minutes,
            default=None,
        )
    except (MapIntegrationError, ValueError):
        return None


def _route_via_parked_car(
    map_service,
    person_location: ItineraryLocation,
    car_location: ItineraryLocation,
    destination: ItineraryLocation,
) -> Optional[Tuple[RouteOption, int]]:
    if map_service is None:
        return None
    walk = _fastest_route(map_service, person_location, car_location, "walking")
    drive = _fastest_route(map_service, car_location, destination, "driving")
    if walk is None or drive is None:
        return None
    path = list(walk.path)
    path.extend(point for point in drive.path if not path or point != path[-1])
    return (
        RouteOption(
            distance_m=walk.distance_m + drive.distance_m,
            duration_minutes=walk.duration_minutes + drive.duration_minutes,
            mode="driving",
            summary="步行取车后驾车",
            path=path,
        ),
        walk.duration_minutes,
    )


def _smart_reason(
    mode: TravelMode,
    parking_minutes: int,
    car_position: Optional[Tuple[float, float]],
    origin_key: Tuple[float, float],
    destination_key: Tuple[float, float],
    origin_label: str,
    retrieval_minutes: int = 0,
) -> str:
    if origin_key == destination_key:
        return "同一地点，无需移动"
    if mode == "driving":
        reason = (
            f"高德推荐驾车，已计入 {parking_minutes} 分钟停车预留"
            if parking_minutes
            else "高德推荐驾车"
        )
        if retrieval_minutes:
            reason = f"先步行 {retrieval_minutes} 分钟取车；" + reason
        return reason
    reason = "短距离推荐步行" if mode == "walking" else "高德推荐公交"
    if car_position == origin_key and destination_key != origin_key:
        reason += f"；车辆停在「{origin_label}」"
    elif car_position is not None and destination_key == car_position:
        reason += "；已回到停车点"
    return reason


def _coordinate_key(location: ItineraryLocation) -> Tuple[float, float]:
    return (
        round(location.coordinate.longitude, 6),
        round(location.coordinate.latitude, 6),
    )


def _late_minutes(leg: ItineraryLeg) -> int:
    if leg.duration_minutes is None or leg.available_minutes is None:
        return 0
    return max(0, leg.duration_minutes - leg.available_minutes)


def _build_leg(
    origin_id: str,
    origin: ItineraryLocation,
    destination: ItineraryItem,
    departure_base: Optional[datetime],
    mode: TravelMode,
    map_service,
    route_option: Optional[RouteOption] = None,
    allow_lookup: bool = True,
    parking_minutes: int = 0,
    recommendation_reason: Optional[str] = None,
    car_location_after: Optional[str] = None,
) -> ItineraryLeg:
    assert destination.location is not None
    direct_path = [origin.coordinate, destination.location.coordinate]
    duration = None
    distance = None
    route_available = False
    path = direct_path

    if route_option is not None:
        duration = route_option.duration_minutes + parking_minutes
        distance = route_option.distance_m
        route_available = True
        path = route_option.path or direct_path
    elif origin.coordinate == destination.location.coordinate:
        duration = 0
        distance = 0
        route_available = True
        path = [origin.coordinate]
    elif allow_lookup and map_service is not None:
        try:
            response = map_service.get_routes(
                origin.coordinate,
                destination.location.coordinate,
                mode,
                destination.location.city or origin.city,
            )
            route = min(
                response.routes,
                key=lambda item: item.duration_minutes,
                default=None,
            )
            if route is not None:
                duration = route.duration_minutes + parking_minutes
                distance = route.distance_m
                route_available = True
                path = route.path or direct_path
        except MapIntegrationError:
            pass

    suggested_departure = None
    estimated_arrival = None
    available_minutes = None
    warning = None
    if duration is not None:
        is_travel = destination.event_type == "travel"
        if departure_base is None and not is_travel:
            suggested_departure = destination.start_time - timedelta(minutes=duration)
            estimated_arrival = destination.start_time
        else:
            if is_travel:
                suggested_departure = max(
                    destination.start_time,
                    departure_base or destination.start_time,
                )
                arrival_deadline = destination.end_time
            else:
                suggested_departure = departure_base
                arrival_deadline = destination.start_time
            estimated_arrival = suggested_departure + timedelta(minutes=duration)
            available_minutes = max(
                0,
                int((arrival_deadline - suggested_departure).total_seconds() // 60),
            )
            if estimated_arrival > arrival_deadline:
                late_minutes = int(
                    (estimated_arrival - arrival_deadline).total_seconds() // 60
                )
                warning = (
                    f"从「{origin.label}」前往「{destination.location.label}」"
                    f"预计会迟到 {late_minutes} 分钟"
                )

    return ItineraryLeg(
        origin_id=origin_id,
        destination_id=destination.id,
        mode=mode,
        distance_m=distance,
        duration_minutes=duration,
        suggested_departure=suggested_departure,
        estimated_arrival=estimated_arrival,
        available_minutes=available_minutes,
        route_available=route_available,
        path=path,
        warning=warning,
        parking_minutes=parking_minutes,
        recommendation_reason=recommendation_reason,
        car_location_after=car_location_after,
    )


def _location_model(location: Optional[SavedLocation]) -> Optional[ItineraryLocation]:
    if location is None:
        return None
    return ItineraryLocation(
        id=location.id,
        label=location.label,
        address=location.address,
        city=location.city,
        coordinate=Coordinate(
            longitude=location.longitude,
            latitude=location.latitude,
        ),
    )


def _custom_location(
    label: Optional[str],
    city: Optional[str],
    longitude: Optional[float],
    latitude: Optional[float],
) -> Optional[ItineraryLocation]:
    if longitude is None or latitude is None:
        return None
    display_name = label or "临时地点"
    return ItineraryLocation(
        label=display_name,
        address=label,
        city=city,
        coordinate=Coordinate(longitude=longitude, latitude=latitude),
    )


def _utc_day_bounds(target_date: date, timezone_name: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    local_start = datetime.combine(target_date, time.min, tzinfo=zone)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc).replace(tzinfo=None),
        local_end.astimezone(timezone.utc).replace(tzinfo=None),
    )
