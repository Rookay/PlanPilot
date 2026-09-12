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

    return {
        "base_url": os.getenv("LLM_BASE_URL") or get("llm", "base_url", ""),
        "api_key": os.getenv("LLM_API_KEY") or get("llm", "api_key", ""),
        "model": os.getenv("LLM_MODEL") or get("llm", "model", "gpt-4o-mini"),
    }


def external_api(name: str) -> dict:
    """读取某个外部 API 的配置，例如 external_api("weather")。"""

    value = section("external").get(name, {})
    return value if isinstance(value, dict) else {}


# 模块导入时加载一次。
reload()
