from unittest import TestCase
from unittest.mock import patch

from app.tools.external_tools import GetWeatherTool
from tests.helpers import make_session


class ExternalToolTests(TestCase):
    def test_weather_tool_returns_structured_forecast(self):
        db = make_session()
        responses = [
            {
                "results": [
                    {
                        "name": "上海",
                        "admin1": "上海",
                        "country": "中国",
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
