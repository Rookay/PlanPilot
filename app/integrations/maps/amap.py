# -*- coding: UTF-8 -*-
"""高德地图 Web 服务适配器。"""

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from app.integrations.maps.base import MapIntegrationError, MapProvider
from app.integrations.maps.schemas import (
    Coordinate,
    MapPlace,
    RouteOption,
    RouteStep,
    TravelMode,
)


class AmapProvider(MapProvider):
    name = "amap"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://restapi.amap.com",
        timeout: int = 10,
        default_city: Optional[str] = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.default_city = default_city

    def search_places(
        self,
        query: str,
        city: Optional[str] = None,
        limit: int = 10,
    ) -> List[MapPlace]:
        params: Dict[str, Any] = {
            "keywords": query,
            "offset": max(1, min(limit, 20)),
            "page": 1,
            "extensions": "base",
        }
        if city or self.default_city:
            params["city"] = city or self.default_city
            params["citylimit"] = "true"
        payload = self._request("/v3/place/text", params)
        return [
            place
            for item in (payload.get("pois") or [])[:limit]
            if (place := self._parse_place(item)) is not None
        ]

    def geocode(self, address: str, city: Optional[str] = None) -> MapPlace:
        params: Dict[str, Any] = {"address": address}
        if city or self.default_city:
            params["city"] = city or self.default_city
        payload = self._request("/v3/geocode/geo", params)
        items = payload.get("geocodes") or []
        if not items:
            raise MapIntegrationError(f"找不到地址「{address}」")
        item = items[0]
        coordinate = self._parse_coordinate(item.get("location"))
        if coordinate is None:
            raise MapIntegrationError("地图服务返回的地址缺少坐标")
        return MapPlace(
            name=_text(item.get("formatted_address")) or address,
            address=_text(item.get("formatted_address")),
            district=_text(item.get("district")),
            city=_text(item.get("city")),
            coordinate=coordinate,
        )

    def get_routes(
        self,
        origin: Coordinate,
        destination: Coordinate,
        mode: TravelMode,
        city: Optional[str] = None,
    ) -> List[RouteOption]:
        params: Dict[str, Any] = {
            "origin": origin.as_provider_text(),
            "destination": destination.as_provider_text(),
        }
        if mode == "walking":
            path = "/v3/direction/walking"
        elif mode == "driving":
            path = "/v3/direction/driving"
            params["extensions"] = "all"
        else:
            path = "/v3/direction/transit/integrated"
            route_city = city or self.default_city
            if not route_city:
                raise MapIntegrationError("公交路线查询需要提供城市")
            params["city"] = route_city
            params["extensions"] = "all"

        payload = self._request(path, params)
        route = payload.get("route") or {}
        if mode == "transit":
            return self._parse_transit_routes(route)
        return self._parse_path_routes(route, mode)

    def _request(self, path: str, params: Dict[str, Any]) -> dict:
        query = urllib.parse.urlencode(
            {**params, "key": self.api_key, "output": "json"}
        )
        request = urllib.request.Request(
            f"{self.base_url}{path}?{query}",
            headers={"User-Agent": "PlanPilot/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise MapIntegrationError(f"地图服务 HTTP {exc.code}: {detail[:120]}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise MapIntegrationError(f"无法连接地图服务：{exc}") from exc

        if payload.get("status") != "1":
            message = _text(payload.get("info")) or "未知错误"
            code = _text(payload.get("infocode"))
            suffix = f"（{code}）" if code else ""
            raise MapIntegrationError(f"高德地图返回错误：{message}{suffix}")
        return payload

    @classmethod
    def _parse_place(cls, item: dict) -> Optional[MapPlace]:
        coordinate = cls._parse_coordinate(item.get("location"))
        if coordinate is None:
            return None
        return MapPlace(
            provider_id=_text(item.get("id")),
            name=_text(item.get("name")) or "未命名地点",
            address=_text(item.get("address")),
            district=_text(item.get("adname")),
            city=_text(item.get("cityname")),
            category=_text(item.get("type")),
            coordinate=coordinate,
        )

    @staticmethod
    def _parse_coordinate(value: Any) -> Optional[Coordinate]:
        text = _text(value)
        if not text or "," not in text:
            return None
        try:
            longitude, latitude = (float(part) for part in text.split(",", 1))
            return Coordinate(longitude=longitude, latitude=latitude)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_path_routes(route: dict, mode: TravelMode) -> List[RouteOption]:
        options: List[RouteOption] = []
        for index, path in enumerate(route.get("paths") or [], start=1):
            route_path = _merge_polylines(
                step.get("polyline") for step in (path.get("steps") or [])
            )
            steps = [
                RouteStep(
                    instruction=_text(step.get("instruction")) or "继续前行",
                    distance_m=_integer(step.get("distance")),
                    duration_minutes=_minutes(step.get("duration")),
                )
                for step in path.get("steps") or []
            ]
            options.append(
                RouteOption(
                    distance_m=_integer(path.get("distance")) or 0,
                    duration_minutes=_minutes(path.get("duration")) or 0,
                    mode=mode,
                    summary=_text(path.get("strategy")) or f"方案 {index}",
                    tolls_cny=_number(path.get("tolls")) if mode == "driving" else None,
                    traffic_lights=(
                        _integer(path.get("traffic_lights"))
                        if mode == "driving"
                        else None
                    ),
                    steps=steps,
                    path=route_path,
                )
            )
        return options

    @staticmethod
    def _parse_transit_routes(route: dict) -> List[RouteOption]:
        options: List[RouteOption] = []
        for index, transit in enumerate(route.get("transits") or [], start=1):
            instructions: List[RouteStep] = []
            line_names: List[str] = []
            route_path: List[Coordinate] = []
            for segment in transit.get("segments") or []:
                walking = segment.get("walking") or {}
                route_path.extend(
                    _merge_polylines(
                        step.get("polyline")
                        for step in (walking.get("steps") or [])
                    )
                )
                if _integer(walking.get("distance")):
                    instructions.append(
                        RouteStep(
                            instruction="步行至换乘点",
                            distance_m=_integer(walking.get("distance")),
                            duration_minutes=_minutes(walking.get("duration")),
                        )
                    )
                for line in ((segment.get("bus") or {}).get("buslines") or []):
                    route_path.extend(_polyline_coordinates(line.get("polyline")))
                    name = _text(line.get("name"))
                    if name:
                        line_names.append(name)
                    instructions.append(
                        RouteStep(
                            instruction=f"乘坐{name or '公共交通'}",
                            distance_m=_integer(line.get("distance")),
                            duration_minutes=_minutes(line.get("duration")),
                        )
                    )
            summary = " → ".join(dict.fromkeys(line_names)) or f"公交方案 {index}"
            options.append(
                RouteOption(
                    distance_m=_integer(transit.get("distance")) or 0,
                    duration_minutes=_minutes(transit.get("duration")) or 0,
                    mode="transit",
                    summary=summary,
                    tolls_cny=_number(transit.get("cost")),
                    steps=instructions,
                    path=_deduplicate_coordinates(route_path),
                )
            )
        return options


def _text(value: Any) -> Optional[str]:
    if isinstance(value, list):
        value = next((item for item in value if item not in (None, "")), None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> Optional[int]:
    number = _number(value)
    return max(0, int(number)) if number is not None else None


def _minutes(seconds: Any) -> Optional[int]:
    number = _number(seconds)
    return max(0, math.ceil(number / 60)) if number is not None else None


def _polyline_coordinates(value: Any) -> List[Coordinate]:
    text = _text(value)
    if not text:
        return []
    coordinates: List[Coordinate] = []
    for item in text.split(";"):
        coordinate = AmapProvider._parse_coordinate(item)
        if coordinate is not None:
            coordinates.append(coordinate)
    return coordinates


def _merge_polylines(values) -> List[Coordinate]:
    merged: List[Coordinate] = []
    for value in values:
        merged.extend(_polyline_coordinates(value))
    return _deduplicate_coordinates(merged)


def _deduplicate_coordinates(values: List[Coordinate]) -> List[Coordinate]:
    result: List[Coordinate] = []
    for value in values:
        if not result or result[-1] != value:
            result.append(value)
    return result
