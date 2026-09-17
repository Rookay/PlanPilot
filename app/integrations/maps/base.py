# -*- coding: UTF-8 -*-
"""地图 Provider 抽象与可识别错误。"""

from abc import ABC, abstractmethod
from typing import List, Optional

from app.integrations.maps.schemas import Coordinate, MapPlace, RouteOption, TravelMode


class MapIntegrationError(RuntimeError):
    """地图服务调用失败。"""


class MapNotConfiguredError(MapIntegrationError):
    """尚未配置地图服务密钥。"""


class MapProvider(ABC):
    name: str = "unknown"

    @abstractmethod
    def search_places(
        self,
        query: str,
        city: Optional[str] = None,
        limit: int = 10,
    ) -> List[MapPlace]:
        ...

    @abstractmethod
    def geocode(self, address: str, city: Optional[str] = None) -> MapPlace:
        ...

    @abstractmethod
    def get_routes(
        self,
        origin: Coordinate,
        destination: Coordinate,
        mode: TravelMode,
        city: Optional[str] = None,
    ) -> List[RouteOption]:
        ...
