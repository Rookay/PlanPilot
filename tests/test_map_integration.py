from unittest import TestCase
from unittest.mock import patch

from app.integrations.maps.amap import AmapProvider
from app.integrations.maps.base import MapNotConfiguredError, MapProvider
from app.integrations.maps.schemas import Coordinate, MapPlace, RouteOption
from app.integrations.maps.service import MapService, build_map_service, map_status


class _FakeProvider(MapProvider):
    name = "fake"

    def __init__(self):
        self.search_count = 0

    def search_places(self, query, city=None, limit=10):
        self.search_count += 1
        return [
            MapPlace(
                provider_id="1",
                name=query,
                city=city,
                coordinate=Coordinate(longitude=121.47, latitude=31.23),
            )
        ][:limit]

    def geocode(self, address, city=None):
        return MapPlace(
            name=address,
            city=city,
            coordinate=Coordinate(longitude=121.47, latitude=31.23),
        )

    def get_routes(self, origin, destination, mode, city=None):
        return [RouteOption(distance_m=1500, duration_minutes=12, mode=mode)]


class MapIntegrationTests(TestCase):
    def test_amap_place_response_is_normalized(self):
        provider = AmapProvider("test-key")
        payload = {
            "status": "1",
            "pois": [
                {
                    "id": "B001",
                    "name": "人民广场",
                    "address": "人民大道",
                    "adname": "黄浦区",
                    "cityname": "上海市",
                    "type": "风景名胜",
                    "location": "121.475000,31.238000",
                }
            ],
        }
        with patch.object(provider, "_request", return_value=payload) as request:
            places = provider.search_places("人民广场", city="上海", limit=5)

        self.assertEqual(len(places), 1)
        self.assertEqual(places[0].provider_id, "B001")
        self.assertEqual(places[0].coordinate.longitude, 121.475)
        self.assertEqual(request.call_args.args[0], "/v3/place/text")

    def test_amap_driving_route_converts_seconds_to_minutes(self):
        provider = AmapProvider("test-key")
        payload = {
            "status": "1",
            "route": {
                "paths": [
                    {
                        "distance": "3200",
                        "duration": "725",
                        "strategy": "速度优先",
                        "tolls": "2.5",
                        "traffic_lights": "8",
                        "steps": [
                            {
                                "instruction": "沿人民大道向东行驶",
                                "distance": "600",
                                "duration": "125",
                                "polyline": "121.470000,31.230000;121.480000,31.240000",
                            }
                        ],
                    }
                ]
            },
        }
        with patch.object(provider, "_request", return_value=payload):
            routes = provider.get_routes(
                Coordinate(longitude=121.47, latitude=31.23),
                Coordinate(longitude=121.50, latitude=31.25),
                "driving",
            )

        self.assertEqual(routes[0].duration_minutes, 13)
        self.assertEqual(routes[0].steps[0].duration_minutes, 3)
        self.assertEqual(routes[0].traffic_lights, 8)
        self.assertEqual(len(routes[0].path), 2)
        self.assertEqual(routes[0].path[-1].longitude, 121.48)

    def test_map_service_caches_place_search(self):
        provider = _FakeProvider()
        service = MapService(provider)

        first = service.search_places("图书馆", "上海", 5)
        second = service.search_places("图书馆", "上海", 5)

        self.assertIs(first, second)
        self.assertEqual(provider.search_count, 1)

    def test_empty_key_is_reported_without_external_request(self):
        config = {"provider": "amap", "api_key": ""}

        status = map_status(config)

        self.assertFalse(status.configured)
        with self.assertRaises(MapNotConfiguredError):
            build_map_service(config)
