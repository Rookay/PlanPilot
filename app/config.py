# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：config.py
"""统一配置加载。

优先级：环境变量 > config.json > 默认值。

config.json 位于项目根目录（含密钥，已加入 .gitignore），
结构见 config.example.json。新增外部 API 时只需在 config.json 的
对应 section 下加字段，然后通过 get() / external_api() 读取。
"""

import json
import os
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.json"

_config: dict = {}


def _load() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def reload() -> None:
    """重新加载 config.json（进程内调用才生效，一般重启即可）。"""

    global _config
    _config = _load()


def get(section: str, key: str, default: Any = None) -> Any:
    return _config.get(section, {}).get(key, default)


def section(name: str) -> dict:
    value = _config.get(name, {})
    return value if isinstance(value, dict) else {}


def llm_config() -> dict:
    """LLM 配置：环境变量优先，其次 config.json，最后默认值。"""

    timeout_value = os.getenv("LLM_TIMEOUT") or get("llm", "timeout", 30)
    try:
        timeout = max(1, min(int(timeout_value), 300))
    except (TypeError, ValueError):
        timeout = 30
    return {
        "base_url": os.getenv("LLM_BASE_URL") or get("llm", "base_url", ""),
        "api_key": os.getenv("LLM_API_KEY") or get("llm", "api_key", ""),
        "model": os.getenv("LLM_MODEL") or get("llm", "model", "gpt-4o-mini"),
        "timeout": timeout,
    }


def external_api(name: str) -> dict:
    """读取某个外部 API 的配置，例如 external_api("weather")。"""

    value = section("external").get(name, {})
    return value if isinstance(value, dict) else {}


def maps_config() -> dict:
    """地图配置：环境变量优先，当前内置高德地图适配器。"""

    configured = external_api("maps")
    timeout_value = os.getenv("MAPS_TIMEOUT") or configured.get("timeout", 10)
    try:
        timeout = max(1, min(int(timeout_value), 60))
    except (TypeError, ValueError):
        timeout = 10
    return {
        "provider": os.getenv("MAPS_PROVIDER") or configured.get("provider", "amap"),
        "base_url": (
            os.getenv("MAPS_BASE_URL")
            or configured.get("base_url", "https://restapi.amap.com")
        ),
        "api_key": os.getenv("MAPS_API_KEY") or configured.get("api_key", ""),
        "timeout": timeout,
        "default_city": (
            os.getenv("MAPS_DEFAULT_CITY") or configured.get("default_city", "")
        ),
        "js_api_key": os.getenv("MAPS_JS_API_KEY") or configured.get("js_api_key", ""),
        "js_security_code": (
            os.getenv("MAPS_JS_SECURITY_CODE")
            or configured.get("js_security_code", "")
        ),
    }


# 模块导入时加载一次。
reload()
