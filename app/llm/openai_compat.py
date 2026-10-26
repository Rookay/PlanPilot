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
import socket
import urllib.error
import urllib.request
from typing import Optional

from app.config import llm_config
from app.llm.base import LLMProvider, LLMResponse, ToolCall
from app.llm.exceptions import LLMAuthError, LLMRateLimitError, LLMTransientError

#: 回传给模型的原始参数片段上限。足够让它看出语法错在哪,又不至于挤爆上下文。
_MAX_RAW_ARGUMENT_CHARS = 200


def _truncate_raw(text: str, limit: int = _MAX_RAW_ARGUMENT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…（原文共 {len(text)} 字符）"


def _parse_arguments(name: str, raw) -> tuple[dict, Optional[str]]:
    """把模型给出的 ``arguments`` 解析成字典,并区分四类情况。

    返回 ``(arguments, parse_error)``:

    - 缺失 / 空串:合法的"无参数"调用 → ``({}, None)``
    - 已经是字典:部分本地模型或代理直接给对象 → 原样接受
    - JSON 语法错误:``({}, 说明)``
    - 合法 JSON 但不是对象(数组、字符串、数字):``({}, 说明)``,
      因为 ``**args`` 只接受字典

    后两类必须保留 ``parse_error``。一旦退化成空字典,模型收到的反馈会变成
    "缺字段",于是它去补字段而不是修语法,而参数全可选的工具甚至会静默执行成
    一次宽松查询。
    """

    if raw is None:
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str):
        return {}, (
            f"{name} 的 arguments 应为 JSON 字符串，实际收到 "
            f"{type(raw).__name__}。请重新输出完整的参数对象。"
        )

    text = raw.strip()
    if not text:
        return {}, None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, (
            f"{name} 的参数不是合法 JSON（{exc.msg}，位置 {exc.pos}）。"
            "请重新输出完整的参数对象，不要只补字段。"
            f"你上次给出的原文：{_truncate_raw(text)}"
        )

    if not isinstance(parsed, dict):
        return {}, (
            f"{name} 的参数必须是 JSON 对象，实际是 {type(parsed).__name__}。"
            f"你上次给出的原文：{_truncate_raw(text)}"
        )

    return parsed, None


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
                name = function.get("name", "")
                arguments, parse_error = _parse_arguments(
                    name, function.get("arguments")
                )
                calls.append(
                    ToolCall(
                        id=raw.get("id") or f"call_{index}",
                        name=name,
                        arguments=arguments,
                        parse_error=parse_error,
                    )
                )
            return LLMResponse(
                tool_calls=calls,
                raw_message=message,
                usage=data.get("usage") or {},
            )

        return LLMResponse(
            text=message.get("content"),
            raw_message=message,
            usage=data.get("usage") or {},
        )

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
                raise _url_error(proxy_error) from proxy_error

            # 某些启动环境会注入已经失效的代理。代理连接失败时再直连一次，
            # 既不影响正常代理，也避免模型永久降级成本地规则。
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                return self._read_response(opener.open, request)
            except urllib.error.HTTPError as exc:
                raise self._http_error(exc) from exc
            except urllib.error.URLError as direct_error:
                raise LLMTransientError(
                    "LLM 连接失败：环境代理不可用，直连也失败："
                    f"{direct_error.reason}"
                ) from direct_error

    def _read_response(self, open_request, request: urllib.request.Request) -> dict:
        with open_request(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError) -> RuntimeError:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code in (401, 403):
            return LLMAuthError(f"LLM 鉴权失败 {exc.code}: {detail}")
        if exc.code == 429:
            return LLMRateLimitError(f"LLM 限流 {exc.code}: {detail}")
        return LLMTransientError(f"LLM 请求失败 {exc.code}: {detail}")


def _url_error(exc: urllib.error.URLError) -> LLMTransientError:
    if isinstance(exc.reason, socket.timeout):
        return LLMTransientError(f"LLM 请求超时: {exc.reason}")
    return LLMTransientError(f"LLM 连接失败: {exc.reason}")


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
