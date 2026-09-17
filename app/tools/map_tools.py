# -*- coding: UTF-8 -*-
"""供 Agent 使用的地图查询工具。"""

from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.integrations.maps.base import MapIntegrationError
from app.integrations.maps.schemas import Coordinate, RouteRequest
from app.integrations.maps.service import get_map_service
from app.tools.base import Tool, ToolResult


class SearchPlacesTool(Tool):
    name = "search_places"
    description = "搜索真实地点并返回候选名称、地址和坐标；地点含糊时先使用此工具。"
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "地点名称或关键词"},
            "city": {"type": "string", "description": "限定城市，可选"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        del db
        try:
            result = get_map_service().search_places(
                str(kwargs.get("query") or ""),
                kwargs.get("city"),
                int(kwargs.get("limit", 5)),
            )
            return ToolResult(
                success=True,
                data=result.model_dump(),
                summary=f"找到 {len(result.places)} 个地点候选",
            )
        except (MapIntegrationError, ValueError, TypeError) as exc:
            return ToolResult(success=False, error=f"地点搜索失败：{exc}")


class GeocodeAddressTool(Tool):
    name = "geocode_address"
    description = "把明确的地址解析为标准地址和经纬度。"
    input_schema = {
        "type": "object",
        "properties": {
            "address": {"type": "string", "description": "完整或较明确的地址"},
            "city": {"type": "string", "description": "地址所在城市，可选"},
        },
        "required": ["address"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        del db
        try:
            result = get_map_service().geocode(
                str(kwargs.get("address") or ""), kwargs.get("city")
            )
            return ToolResult(
                success=True,
                data=result.model_dump(),
                summary=f"已定位：{result.place.name}",
            )
        except (MapIntegrationError, ValueError) as exc:
            return ToolResult(success=False, error=f"地址解析失败：{exc}")


class EstimateTravelTimeTool(Tool):
    name = "estimate_travel_time"
    description = "按步行、驾车或公交路线估算两组坐标之间的真实距离和耗时。"
    input_schema = {
        "type": "object",
        "properties": {
            "origin": {"$ref": "#/$defs/coordinate"},
            "destination": {"$ref": "#/$defs/coordinate"},
            "mode": {
                "type": "string",
                "enum": ["walking", "driving", "transit"],
                "default": "transit",
            },
            "city": {"type": "string", "description": "公交查询所需城市，可选"},
        },
        "required": ["origin", "destination"],
        "additionalProperties": False,
        "$defs": {
            "coordinate": {
                "type": "object",
                "properties": {
                    "longitude": {"type": "number", "minimum": -180, "maximum": 180},
                    "latitude": {"type": "number", "minimum": -90, "maximum": 90},
                },
                "required": ["longitude", "latitude"],
                "additionalProperties": False,
            }
        },
    }
    output_schema = {"type": "object"}
    confirmation = "none"

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        del db
        try:
            request = RouteRequest(
                origin=Coordinate.model_validate(kwargs.get("origin")),
                destination=Coordinate.model_validate(kwargs.get("destination")),
                mode=kwargs.get("mode", "transit"),
                city=kwargs.get("city"),
            )
            result = get_map_service().get_routes(
                request.origin,
                request.destination,
                request.mode,
                request.city,
            )
            fastest = min(result.routes, key=lambda item: item.duration_minutes, default=None)
            summary = (
                f"最快方案约 {fastest.duration_minutes} 分钟，{fastest.distance_m} 米"
                if fastest
                else "未找到可用路线"
            )
            return ToolResult(success=True, data=result.model_dump(), summary=summary)
        except (MapIntegrationError, ValidationError, ValueError, TypeError) as exc:
            return ToolResult(success=False, error=f"路线估算失败：{exc}")
