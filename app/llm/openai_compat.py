# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：openai_compat.py
"""OpenAI 兼容 Provider（真实大模型接入）。

不依赖任何 SDK，直接用标准库 urllib 调用 ``/chat/completions`` 接口，
兼容 OpenAI、DeepSeek、通义千问（DashScope 兼容模式）、Ollama、vLLM 等。

通过环境变量配置：
- LLM_BASE_URL   例如 https://api.openai.com/v1
- LLM_API_KEY    API Key（Ollama 等本地服务可留空）
- LLM_MODEL      例如 gpt-4o-mini / deepseek-chat / qwen-plus
"""

import json
import os
import urllib.error
import urllib.request
from typing import Optional

from app.config import llm_config
from app.llm.base import LLMProvider, LLMResponse, ToolCall


class OpenAICompatibleProvider(LLMProvider):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
    ) -> LLMResponse:
        payload: dict = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        data = self._post("/chat/completions", payload)
        message = data["choices"][0]["message"]

        raw_calls = message.get("tool_calls")
        if raw_calls:
            calls = []
            for index, raw in enumerate(raw_calls):
                function = raw.get("function", {})
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                calls.append(
                    ToolCall(
                        id=raw.get("id") or f"call_{index}",
                        name=function.get("name", ""),
                        arguments=arguments,
                    )
                )
            return LLMResponse(tool_calls=calls, raw_message=message)

        return LLMResponse(text=message.get("content"), raw_message=message)

    def structured(self, messages: list[dict], output_schema: dict) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        data = self._post("/chat/completions", payload)
        content = data["choices"][0]["message"].get("content", "{}")
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {}

    # ------------------------------------------------------------------

    def _post(self, path: str, payload: dict) -> dict:
        url = self.base_url + path
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )

        try:
            return self._read_response(urllib.request.urlopen, request)
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except urllib.error.URLError as proxy_error:
            if not _has_environment_proxy():
                raise RuntimeError(f"LLM 连接失败: {proxy_error.reason}") from proxy_error

            # 某些启动环境会注入已经失效的代理。代理连接失败时再直连一次，
            # 既不影响正常代理，也避免模型永久降级成本地规则。
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                return self._read_response(opener.open, request)
            except urllib.error.HTTPError as exc:
                raise self._http_error(exc) from exc
            except urllib.error.URLError as direct_error:
                raise RuntimeError(
                    "LLM 连接失败：环境代理不可用，直连也失败："
                    f"{direct_error.reason}"
                ) from direct_error

    def _read_response(self, open_request, request: urllib.request.Request) -> dict:
        with open_request(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError) -> RuntimeError:
        detail = exc.read().decode("utf-8", errors="replace")
        return RuntimeError(f"LLM 请求失败 {exc.code}: {detail}")


def _has_environment_proxy() -> bool:
    return any(
        os.getenv(name)
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    )


def from_config() -> OpenAICompatibleProvider | None:
    """从 config.json / 环境变量构建 Provider；未配置 base_url 时返回 None。"""

    config = llm_config()
    base_url = config["base_url"]
    if not base_url:
        return None
    return OpenAICompatibleProvider(
        base_url=base_url,
        api_key=config["api_key"],
        model=config["model"],
        timeout=config["timeout"],
    )
