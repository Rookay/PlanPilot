# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：external_tools.py
"""时间与天气等外部工具。"""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.services import preference_service
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
    description = "按城市查询某日天气、温度和降水概率，用于判断户外安排。"
    input_schema = {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
            "location": {"type": "string", "description": "城市，可选"},
        },
        "required": ["date"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        target_date = str(kwargs.get("date", "")).strip()
        try:
            date.fromisoformat(target_date)
        except ValueError:
            return ToolResult(success=False, error="日期必须使用 YYYY-MM-DD 格式")

        preferences = preference_service.get_preferences(db)
        location = (kwargs.get("location") or preferences.default_location).strip()
        try:
            place = self._geocode(location)
            forecast = self._forecast(place, target_date)
        except (RuntimeError, urllib.error.URLError, TimeoutError) as exc:
            return ToolResult(success=False, error=f"天气服务暂时不可用：{exc}")

        condition = _weather_label(forecast["weather_code"])
        data = {
            "date": target_date,
            "location": place["display_name"],
            "condition": condition,
            "temperature_min": forecast["temperature_2m_min"],
            "temperature_max": forecast["temperature_2m_max"],
            "precipitation_probability_max": forecast[
                "precipitation_probability_max"
            ],
            "precipitation_sum": forecast["precipitation_sum"],
            "source": "Open-Meteo",
        }
        return ToolResult(
            success=True,
            data=data,
            summary=(
                f"{place['display_name']} {target_date}：{condition}，"
                f"{forecast['temperature_2m_min']}~{forecast['temperature_2m_max']}℃，"
                f"最高降水概率 {forecast['precipitation_probability_max']}%"
            ),
        )

    @staticmethod
    def _geocode(location: str) -> dict:
        query = urllib.parse.urlencode(
            {"name": location, "count": 1, "language": "zh", "format": "json"}
        )
        payload = _get_json(f"https://geocoding-api.open-meteo.com/v1/search?{query}")
        results = payload.get("results") or []
        if not results:
            raise RuntimeError(f"找不到地点「{location}」")
        item = results[0]
        display = "，".join(
            part for part in (item.get("name"), item.get("admin1"), item.get("country")) if part
        )
        return {
            "latitude": item["latitude"],
            "longitude": item["longitude"],
            "display_name": display or location,
        }

    @staticmethod
    def _forecast(place: dict, target_date: str) -> dict:
        query = urllib.parse.urlencode(
            {
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_sum,precipitation_probability_max"
                ),
                "timezone": "auto",
                "start_date": target_date,
                "end_date": target_date,
            }
        )
        payload = _get_json(f"https://api.open-meteo.com/v1/forecast?{query}")
        daily = payload.get("daily") or {}
        if not daily.get("time"):
            raise RuntimeError("没有该日期的预报数据")
        result = {
            key: values[0]
            for key, values in daily.items()
            if key != "time" and isinstance(values, list) and values
        }
        required = {
            "weather_code",
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_sum",
            "precipitation_probability_max",
        }
        if not required.issubset(result):
            raise RuntimeError("天气服务返回的数据不完整")
        return result


def _get_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "PlanPilot/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:160]}") from exc


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
