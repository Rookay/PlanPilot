"""地图服务统一入口。"""

from app.integrations.maps.service import MapService, get_map_service

__all__ = ["MapService", "get_map_service"]
