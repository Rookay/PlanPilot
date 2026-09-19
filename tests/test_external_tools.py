import urllib.error
from unittest import TestCase
from unittest.mock import MagicMock, patch

from app.schemas.location import SavedLocationCreate
from app.services import location_service
from app.tools.external_tools import GetWeatherTool, _get_json
from tests.helpers import make_session


class ExternalToolTests(TestCase):
    def test_external_json_request_retries_one_transient_failure(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        with patch(
            "app.tools.external_tools.urllib.request.urlopen",
            side_effect=[urllib.error.URLError("temporary"), response],
        ) as urlopen, patch("app.tools.external_tools.time.sleep"):
            payload = _get_json("https://example.test/weather")

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)

    def test_weather_tool_returns_structured_forecast(self):
        db = make_session()
        responses = [
            {
                "results": [
                    {
                        "name": "上海",
                        "admin1": "上海",
                        "country": "中国",
                        "country_code": "CN",
                        "feature_code": "PPLA",
                        "population": 24000000,
                        "latitude": 31.23,
                        "longitude": 121.47,
                    }
                ]
            },
            {
                "daily": {
                    "time": ["2026-09-18"],
                    "weather_code": [61],
                    "temperature_2m_max": [25.0],
                    "temperature_2m_min": [19.0],
                    "precipitation_sum": [4.2],
                    "precipitation_probability_max": [80],
                }
            },
        ]
        with patch("app.tools.external_tools._get_json", side_effect=responses):
            result = GetWeatherTool().execute(db, date="2026-09-18", location="上海")
        db.close()

        self.assertTrue(result.success)
        self.assertEqual(result.data["condition"], "有雨")
        self.assertEqual(result.data["precipitation_probability_max"], 80)

    def test_weather_tool_queries_a_date_range_in_one_forecast_request(self):
        db = make_session()
        responses = [
            {
                "results": [
                    {
                        "name": "阜阳",
                        "admin1": "安徽",
                        "country": "中国",
                        "country_code": "CN",
                        "feature_code": "PPLA2",
                        "population": 8200000,
                        "latitude": 32.90,
                        "longitude": 115.82,
                    }
                ]
            },
            {
                "daily": {
                    "time": ["2026-09-21", "2026-09-22", "2026-09-23"],
                    "weather_code": [61, 3, 0],
                    "temperature_2m_max": [25.0, 26.0, 27.0],
                    "temperature_2m_min": [18.0, 18.0, 19.0],
                    "precipitation_sum": [5.0, 0.2, 0.0],
                    "precipitation_probability_max": [80, 20, 5],
                }
            },
        ]
        with patch("app.tools.external_tools._get_json", side_effect=responses) as get_json:
            result = GetWeatherTool().execute(
                db,
                start_date="2026-09-21",
                end_date="2026-09-23",
                location="阜阳",
            )
        db.close()

        self.assertTrue(result.success)
        self.assertEqual(len(result.data["forecasts"]), 3)
        self.assertEqual(result.data["forecasts"][-1]["condition"], "晴")
        self.assertIn("2026-09-23 降水风险最低", result.summary)
        self.assertEqual(get_json.call_count, 2)

    def test_weather_geocoding_prefers_the_major_same_name_city(self):
        candidates = {
            "results": [
                {
                    "name": "阜阳",
                    "admin1": "江苏",
                    "country": "中国",
                    "country_code": "CN",
                    "feature_code": "PPL",
                    "population": 8000,
                    "latitude": 31.0,
                    "longitude": 119.0,
                },
                {
                    "name": "阜阳市",
                    "admin1": "安徽省",
                    "country": "中国",
                    "country_code": "CN",
                    "feature_code": "PPLA2",
                    "population": 8200000,
                    "latitude": 32.90,
                    "longitude": 115.82,
                },
            ]
        }
        with patch("app.tools.external_tools._get_json", return_value=candidates):
            place = GetWeatherTool._geocode("阜阳")

        self.assertEqual(place["display_name"], "阜阳市，安徽省，中国")
        self.assertEqual(place["longitude"], 115.82)

    def test_weather_uses_saved_home_coordinates_before_city_geocoding(self):
        db = make_session()
        location_service.create_location(
            db,
            SavedLocationCreate(
                label="家",
                address="安徽省阜阳市阜南县王店孜乡",
                city="阜阳市",
                longitude=115.576362,
                latitude=32.79848,
            ),
        )
        weather_response = {
            "daily": {
                "time": ["2026-09-23"],
                "weather_code": [3],
                "temperature_2m_max": [29.0],
                "temperature_2m_min": [19.0],
                "precipitation_sum": [0.0],
                "precipitation_probability_max": [6],
            }
        }
        with patch(
            "app.tools.external_tools._get_json",
            return_value=weather_response,
        ) as get_json:
            result = GetWeatherTool().execute(
                db,
                date="2026-09-23",
                location="阜阳",
            )
        db.close()

        self.assertTrue(result.success)
        self.assertEqual(result.data["location"], "阜阳市，安徽省，中国")
        self.assertEqual(get_json.call_count, 1)
        forecast_url = get_json.call_args.args[0]
        self.assertIn("latitude=32.8", forecast_url)
        self.assertIn("longitude=115.6", forecast_url)
        self.assertNotIn("115.576362", forecast_url)
