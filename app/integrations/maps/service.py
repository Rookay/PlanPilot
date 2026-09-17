# -*- coding: UTF-8 -*-
"""地图能力门面：隐藏 Provider 差异并提供轻量缓存。"""

import json
from threading import Lock
from typing import Any, Dict, Optional

from app.config import maps_config
from app.integrations.maps.amap import AmapProvider
from app.integrations.maps.base import MapNotConfiguredError, MapProvider
from app.integrations.maps.cache import TTLCache
from app.integrations.maps.schemas import (
    Coordinate,
    GeocodeResponse,
    MapStatusResponse,
    PlaceSearchResponse,
    RouteResponse,
    TravelMode,
)


class MapService:
    def __init__(self, provider: MapProvider, cache: Optional[TTLCache] = None):
        self.provider = provider
        self.cache = cache or TTLCache()

    def search_places(
        self,
        query: str,
        city: Optional[str] = None,
        limit: int = 10,
    ) -> PlaceSearchResponse:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("搜索关键词不能为空")
        safe_limit = max(1, min(limit, 20))
        key = self._cache_key("places", normalized_query, city, safe_limit)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        result = PlaceSearchResponse(
            query=normalized_query,
            provider=self.provider.name,
            places=self.provider.search_places(normalized_query, city, safe_limit),
        )
        self.cache.set(key, result, ttl_seconds=24 * 60 * 60)
        return result

    def geocode(self, address: str, city: Optional[str] = None) -> GeocodeResponse:
        normalized_address = address.strip()
        if not normalized_address:
            raise ValueError("地址不能为空")
        key = self._cache_key("geocode", normalized_address, city)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        result = GeocodeResponse(
            provider=self.provider.name,
            place=self.provider.geocode(normalized_address, city),
        )
        self.cache.set(key, result, ttl_seconds=30 * 24 * 60 * 60)
        return result

    def get_routes(
        self,
        origin: Coordinate,
        destination: Coordinate,
        mode: TravelMode = "transit",
        city: Optional[str] = None,
    ) -> RouteResponse:
        key = self._cache_key(
            "routes",
            origin.model_dump(),
            destination.model_dump(),
            mode,
            city,
        )
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        result = RouteResponse(
            provider=self.provider.name,
            origin=origin,
            destination=destination,
            routes=self.provider.get_routes(origin, destination, mode, city),
        )
        self.cache.set(key, result, ttl_seconds=5 * 60)
        return result

    @staticmethod
    def _cache_key(operation: str, *values: Any) -> str:
        return f"{operation}:{json.dumps(values, ensure_ascii=False, sort_keys=True)}"


def map_status(config: Optional[Dict[str, Any]] = None) -> MapStatusResponse:
    settings = config if config is not None else maps_config()
    provider = str(settings.get("provider") or "amap").strip().lower()
    has_key = bool(str(settings.get("api_key") or "").strip())
    supported = provider == "amap"
    configured = has_key and supported
    if not supported:
        message = f"暂不支持地图服务商「{provider}」"
    elif has_key:
        message = "地图服务已配置"
    else:
        message = "尚未配置地图服务 Key；填入 Key 后即可启用"
    return MapStatusResponse(
        configured=configured,
        provider=provider,
        capabilities=["place_search", "geocoding", "route_estimation"],
        message=message,
    )


def build_map_service(config: Optional[Dict[str, Any]] = None) -> MapService:
    settings = config if config is not None else maps_config()
    provider_name = str(settings.get("provider") or "amap").strip().lower()
    api_key = str(settings.get("api_key") or "").strip()
    if not api_key:
        raise MapNotConfiguredError("尚未配置高德地图 Web 服务 Key")
    if provider_name != "amap":
        raise MapNotConfiguredError(f"暂不支持地图服务商「{provider_name}」")
    provider = AmapProvider(
        api_key=api_key,
        base_url=str(settings.get("base_url") or "https://restapi.amap.com"),
        timeout=int(settings.get("timeout") or 10),
        default_city=str(settings.get("default_city") or "").strip() or None,
    )
    return MapService(provider)


_service_lock = Lock()
_service_signature: Optional[str] = None
_service: Optional[MapService] = None


def get_map_service() -> MapService:
    """按当前配置复用服务；配置变化后自动重建。"""

    global _service, _service_signature
    settings = maps_config()
    signature = json.dumps(settings, ensure_ascii=False, sort_keys=True)
    with _service_lock:
        if _service is None or signature != _service_signature:
            _service = build_map_service(settings)
            _service_signature = signature
        return _service
