# -*- coding: UTF-8 -*-
"""与具体地图厂商无关的标准数据模型。"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

TravelMode = Literal["walking", "driving", "transit"]


class Coordinate(BaseModel):
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)

    def as_provider_text(self) -> str:
        return f"{self.longitude:.6f},{self.latitude:.6f}"


class MapPlace(BaseModel):
    provider_id: Optional[str] = None
    name: str
    address: Optional[str] = None
    district: Optional[str] = None
    city: Optional[str] = None
    category: Optional[str] = None
    coordinate: Coordinate


class PlaceSearchResponse(BaseModel):
    query: str
    provider: str
    places: List[MapPlace] = Field(default_factory=list)


class GeocodeRequest(BaseModel):
    address: str = Field(min_length=1, max_length=200)
    city: Optional[str] = Field(default=None, max_length=100)


class GeocodeResponse(BaseModel):
    provider: str
    place: MapPlace


class RouteRequest(BaseModel):
    origin: Coordinate
    destination: Coordinate
    mode: TravelMode = "transit"
    city: Optional[str] = Field(default=None, max_length=100)


class RouteStep(BaseModel):
    instruction: str
    distance_m: Optional[int] = None
    duration_minutes: Optional[int] = None


class RouteOption(BaseModel):
    distance_m: int = Field(ge=0)
    duration_minutes: int = Field(ge=0)
    mode: TravelMode
    summary: Optional[str] = None
    tolls_cny: Optional[float] = None
    traffic_lights: Optional[int] = None
    steps: List[RouteStep] = Field(default_factory=list)
    path: List[Coordinate] = Field(default_factory=list)


class RouteResponse(BaseModel):
    provider: str
    origin: Coordinate
    destination: Coordinate
    routes: List[RouteOption] = Field(default_factory=list)


class MapStatusResponse(BaseModel):
    configured: bool
    provider: str
    capabilities: List[str]
    message: str


class MapClientConfigResponse(BaseModel):
    configured: bool
    provider: str
    js_api_key: Optional[str] = None
    js_security_code: Optional[str] = None
    message: str
