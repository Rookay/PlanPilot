import json
import os
import urllib.error
from unittest import TestCase
from unittest.mock import Mock, patch

from app.llm.openai_compat import OpenAICompatibleProvider


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(
            {
                "choices": [{"message": {"content": "OK"}}],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                    "total_tokens": 15,
                },
            }
        ).encode("utf-8")


class OpenAICompatibleProviderTests(TestCase):
    def test_failed_environment_proxy_retries_without_proxy(self):
        provider = OpenAICompatibleProvider(
            base_url="https://example.test/v1",
            api_key="test-key",
            model="test-model",
        )
        opener = Mock()
        opener.open.return_value = _Response()

        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9"}), patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(ConnectionRefusedError()),
        ), patch("urllib.request.build_opener", return_value=opener) as build_opener:
            response = provider.chat([{"role": "user", "content": "hello"}])

        self.assertEqual(response.text, "OK")
        self.assertEqual(response.usage["total_tokens"], 15)
        build_opener.assert_called_once()
        opener.open.assert_called_once()
