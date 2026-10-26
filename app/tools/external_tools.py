# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：external_tools.py
"""时间与天气等外部工具。"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.maps.base import MapIntegrationError
from app.integrations.maps.service import get_map_service
from app.services import location_service, preference_service
from app.tools.base import Tool, ToolResult
from app.utils import local_now, utc_offset_hours, utcnow

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _get_timezone(db: Session) -> str:
    return preference_service.get_preferences(db).timezone


class GetCurrentTimeTool(Tool):
    name = "get_current_time"
    description = (
        "获取当前时间（同时返回 UTC 和用户本地时间）。当用户提到「今天/明天/现在」等相对时间，"
        "或需要以当前时间为基准进行推算时使用。注意：传给其它工具的时间必须是 UTC。"
    )
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        timezone = _get_timezone(db)
        now = utcnow()
        local = local_now(timezone)
        offset = utc_offset_hours(timezone)
        return ToolResult(
            success=True,
            data={
                "utc_now": now.isoformat() + "Z",
                "local_now": local.isoformat(),
                "date": local.strftime("%Y-%m-%d"),
                "time": local.strftime("%H:%M:%S"),
                "weekday": _WEEKDAYS[local.weekday()],
                "iso_weekday": local.isoweekday(),
                "timezone": timezone,
                "utc_offset_hours": offset,
            },
        )


class GetWeatherTool(Tool):
    name = "get_weather"
    description = (
        "按城市查询单日或连续日期范围的天气、温度和降水概率。比较多天时必须使用 "
        "start_date/end_date 一次查询，禁止逐日重复调用。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "单日查询，YYYY-MM-DD"},
            "start_date": {"type": "string", "description": "范围开始日期，YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "范围结束日期，YYYY-MM-DD，最多连续 14 天"},
            "location": {
                "type": "string",
                "description": "城市，可带省份消歧，例如“安徽省阜阳市”；可选",
            },
            "longitude": {
                "type": "number",
                "minimum": -180,
                "maximum": 180,
                "description": "城市经度；已知坐标时可用它代替 location",
            },
            "latitude": {
                "type": "number",
                "minimum": -90,
                "maximum": 90,
                "description": "城市纬度；配合 longitude 使用",
            },
        },
        "required": [],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        try:
            start_date, end_date = self._date_range(kwargs)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

        preferences = preference_service.get_preferences(db)
        location = (kwargs.get("location") or preferences.default_location).strip()
        try:
            place = self._resolve_place(db, location, kwargs)
            forecasts = self._forecasts(place, start_date, end_date)
        except (RuntimeError, urllib.error.URLError, TimeoutError) as exc:
            return ToolResult(success=False, error=f"天气服务暂时不可用：{exc}")

        if len(forecasts) == 1:
            data = {
                **forecasts[0],
                "location": place["display_name"],
                "source": "Open-Meteo",
            }
            summary = _forecast_summary(place["display_name"], forecasts[0])
        else:
            data = {
                "location": place["display_name"],
                "start_date": start_date,
                "end_date": end_date,
                "forecasts": forecasts,
                "source": "Open-Meteo",
            }
            driest = min(
                forecasts,
                key=lambda item: (
                    item["precipitation_probability_max"],
                    item["precipitation_sum"],
                ),
            )
            summary = (
                f"{place['display_name']} {start_date} 至 {end_date} 共 {len(forecasts)} 天；"
                f"其中 {driest['date']} 降水风险最低 "
                f"({driest['precipitation_probability_max']}%)"
            )
        return ToolResult(
            success=True,
            data=data,
            summary=summary,
        )

    @staticmethod
    def _date_range(kwargs: dict) -> tuple[str, str]:
        single = str(kwargs.get("date") or "").strip()
        start_value = str(kwargs.get("start_date") or "").strip()
        end_value = str(kwargs.get("end_date") or "").strip()
        if single:
            start_value = end_value = single
        elif not start_value or not end_value:
            raise ValueError("请提供 date，或同时提供 start_date 和 end_date")
        try:
            start = date.fromisoformat(start_value)
            end = date.fromisoformat(end_value)
        except ValueError as exc:
            raise ValueError("日期必须使用 YYYY-MM-DD 格式") from exc
        if end < start:
            raise ValueError("结束日期不能早于开始日期")
        if end - start > timedelta(days=13):
            raise ValueError("天气日期范围最多连续 14 天")
        return start.isoformat(), end.isoformat()

    @classmethod
    def _resolve_place(cls, db: Session, location: str, kwargs: dict) -> dict:
        longitude = kwargs.get("longitude")
        latitude = kwargs.get("latitude")
        if longitude is not None and latitude is not None:
            return {
                "latitude": _city_level_coordinate(latitude),
                "longitude": _city_level_coordinate(longitude),
                "display_name": location,
                "source": "coordinates",
            }

        saved = _match_saved_location(db, location)
        if saved is not None:
            return {
                # Weather does not need a household-level coordinate. Keep only
                # city-level precision before sending it to the forecast provider.
                "latitude": _city_level_coordinate(saved.latitude),
                "longitude": _city_level_coordinate(saved.longitude),
                "display_name": _saved_location_display(saved),
                "source": "saved_location",
            }

        place = cls._geocode(location)
        if not _is_minor_geocoding_result(place):
            return place

        try:
            geocoded = get_map_service().geocode(location, location)
            map_place = geocoded.place
            display = "，".join(
                dict.fromkeys(
                    part
                    for part in (map_place.city, map_place.district, map_place.name)
                    if part
                )
            )
            return {
                "latitude": map_place.coordinate.latitude,
                "longitude": map_place.coordinate.longitude,
                "display_name": display or location,
                "source": geocoded.provider,
            }
        except (MapIntegrationError, ValueError, TypeError):
            return place

    @staticmethod
    def _geocode(location: str) -> dict:
        query = urllib.parse.urlencode(
            {"name": location, "count": 10, "language": "zh", "format": "json"}
        )
        payload = _get_json(f"https://geocoding-api.open-meteo.com/v1/search?{query}")
        results = payload.get("results") or []
        if not results:
            raise RuntimeError(f"找不到地点「{location}」")
        item = _select_geocoding_result(results, location)
        display = "，".join(
            part for part in (item.get("name"), item.get("admin1"), item.get("country")) if part
        )
        return {
            "latitude": item["latitude"],
            "longitude": item["longitude"],
            "display_name": display or location,
            "feature_code": item.get("feature_code"),
            "population": item.get("population"),
            "source": "open_meteo_geocoding",
        }

    @staticmethod
    def _forecasts(place: dict, start_date: str, end_date: str) -> list[dict]:
        query = urllib.parse.urlencode(
            {
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_sum,precipitation_probability_max"
                ),
                "timezone": "auto",
                "start_date": start_date,
                "end_date": end_date,
            }
        )
        payload = _get_json(f"https://api.open-meteo.com/v1/forecast?{query}")
        daily = payload.get("daily") or {}
        dates = daily.get("time") or []
        if not dates:
            raise RuntimeError("没有该日期的预报数据")
        required = {
            "weather_code",
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_sum",
            "precipitation_probability_max",
        }
        if not required.issubset(daily) or any(
            not isinstance(daily[key], list) or len(daily[key]) != len(dates)
            for key in required
        ):
            raise RuntimeError("天气服务返回的数据不完整")
        return [
            {
                "date": forecast_date,
                "condition": _weather_label(daily["weather_code"][index]),
                "temperature_min": daily["temperature_2m_min"][index],
                "temperature_max": daily["temperature_2m_max"][index],
                "precipitation_probability_max": daily[
                    "precipitation_probability_max"
                ][index],
                "precipitation_sum": daily["precipitation_sum"][index],
            }
            for index, forecast_date in enumerate(dates)
        ]


def _forecast_summary(location: str, forecast: dict) -> str:
    return (
        f"{location} {forecast['date']}：{forecast['condition']}，"
        f"{forecast['temperature_min']}~{forecast['temperature_max']}℃，"
        f"最高降水概率 {forecast['precipitation_probability_max']}%"
    )


def _city_level_coordinate(value: Any) -> float:
    return round(float(value), 1)


def _match_saved_location(db: Session, query: str):
    normalized_query = _normalize_location(query)
    ranked = []
    for saved in location_service.list_locations(db):
        label = _normalize_location(saved.label)
        city = _normalize_location(saved.city)
        address = _normalize_location(saved.address)
        score = 0
        if label and label == normalized_query:
            score = 300
        elif city and city == normalized_query:
            score = 260
        elif city and (city in normalized_query or normalized_query in city):
            score = 220
        elif label and label in normalized_query:
            score = 200
        elif normalized_query and normalized_query in address:
            score = 180
        if score:
            ranked.append((score, int(saved.is_default), -saved.id, saved))
    return max(ranked, default=(0, 0, 0, None), key=lambda item: item[:3])[3]


def _saved_location_display(saved) -> str:
    address = str(saved.address or "")
    province = ""
    for suffix in ("特别行政区", "自治区", "省"):
        index = address.find(suffix)
        if 0 <= index <= 8:
            province = address[: index + len(suffix)]
            break
    parts = list(
        dict.fromkeys(
            part for part in (saved.city, province, "中国") if part
        )
    )
    return "，".join(parts) or saved.label


def _is_minor_geocoding_result(place: dict) -> bool:
    feature_code = str(place.get("feature_code") or "")
    try:
        population = int(place.get("population") or 0)
    except (TypeError, ValueError):
        population = 0
    return feature_code not in {"PPLC", "PPLA", "PPLA2", "PPLA3"} and population < 50000


def _select_geocoding_result(results: list[dict], location: str) -> dict:
    """Prefer the intended city over a small same-name locality."""

    query = _normalize_location(location)
    feature_rank = {"PPLC": 4, "PPLA": 3, "PPLA2": 2, "PPLA3": 1}

    def rank(index_and_item: tuple[int, dict]) -> tuple[int, int, int, int]:
        index, item = index_and_item
        name = _normalize_location(item.get("name"))
        fields = [
            name,
            _normalize_location(item.get("admin1")),
            _normalize_location(item.get("admin2")),
            _normalize_location(item.get("country")),
        ]
        searchable = "".join(fields)
        score = 0
        if name and name == query:
            score += 120
        elif name and name in query:
            score += 90
        elif query and query in searchable:
            score += 60
        score += 35 * sum(bool(part and part in query) for part in fields[1:])
        score += 10 if str(item.get("country_code") or "").upper() == "CN" else 0
        feature_score = feature_rank.get(str(item.get("feature_code") or ""), 0)
        try:
            population = int(item.get("population") or 0)
        except (TypeError, ValueError):
            population = 0
        return score, feature_score, population, -index

    return max(enumerate(results), key=rank)[1]


def _normalize_location(value: Any) -> str:
    normalized = str(value or "").lower()
    for token in (
        "中华人民共和国",
        "中国",
        "省",
        "市",
        "地区",
        "自治州",
        "区",
        "县",
        " ",
        "，",
        ",",
    ):
        normalized = normalized.replace(token, "")
    return normalized


def _get_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "PlanPilot/1.0"},
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail[:160]}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt:
                raise
            time.sleep(0.15)
    raise RuntimeError("外部服务未返回数据")


def _weather_label(code: int | float | None) -> str:
    if code == 0:
        return "晴"
    if code in {1, 2, 3}:
        return "多云"
    if code in {45, 48}:
        return "有雾"
    if code in {51, 53, 55, 56, 57}:
        return "毛毛雨"
    if code in {61, 63, 65, 66, 67, 80, 81, 82}:
        return "有雨"
    if code in {71, 73, 75, 77, 85, 86}:
        return "有雪"
    if code in {95, 96, 99}:
        return "雷雨"
    return "天气状况未知"
