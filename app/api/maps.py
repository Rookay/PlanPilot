# -*- coding: UTF-8 -*-
"""地点搜索、地址解析与路线估算接口。"""

from fastapi import APIRouter, HTTPException, Query

from app.integrations.maps.base import MapIntegrationError, MapNotConfiguredError
from app.integrations.maps.schemas import (
    GeocodeRequest,
    GeocodeResponse,
    MapStatusResponse,
    MapClientConfigResponse,
    PlaceSearchResponse,
    RouteRequest,
    RouteResponse,
)
from app.integrations.maps.service import get_map_service, map_status
from app.config import maps_config

router = APIRouter(prefix="/api/maps", tags=["Maps"])


@router.get("/status", response_model=MapStatusResponse)
def get_status():
    return map_status()


@router.get("/client-config", response_model=MapClientConfigResponse)
def get_client_config():
    settings = maps_config()
    key = str(settings.get("js_api_key") or "").strip()
    security_code = str(settings.get("js_security_code") or "").strip()
    configured = bool(key and security_code)
    return MapClientConfigResponse(
        configured=configured,
        provider=str(settings.get("provider") or "amap"),
        js_api_key=key or None,
        js_security_code=security_code or None,
        message=(
            "地图展示已配置"
            if configured
            else "尚未配置高德 JS API Key 和安全密钥"
        ),
    )


@router.get("/places", response_model=PlaceSearchResponse)
def search_places(
    q: str = Query(min_length=1, max_length=100),
    city: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=10, ge=1, le=20),
):
    try:
        return get_map_service().search_places(q, city, limit)
    except (MapIntegrationError, ValueError) as exc:
        _raise_map_error(exc)


@router.post("/geocode", response_model=GeocodeResponse)
def geocode(payload: GeocodeRequest):
    try:
        return get_map_service().geocode(payload.address, payload.city)
    except (MapIntegrationError, ValueError) as exc:
        _raise_map_error(exc)


@router.post("/routes", response_model=RouteResponse)
def routes(payload: RouteRequest):
    try:
        return get_map_service().get_routes(
            payload.origin,
            payload.destination,
            payload.mode,
            payload.city,
        )
    except (MapIntegrationError, ValueError) as exc:
        _raise_map_error(exc)


def _raise_map_error(exc: Exception) -> None:
    status_code = 503 if isinstance(exc, MapNotConfiguredError) else 502
    if isinstance(exc, ValueError):
        status_code = 400
    raise HTTPException(status_code=status_code, detail=str(exc)) from exc
