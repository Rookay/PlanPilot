# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：registry.py
"""Tool Registry：工具注册、schema 汇总、统一执行入口。"""

from typing import Dict, List

from app.tools.base import Tool, ToolResult


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具，以 tool.name 作为唯一键。"""

        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def schemas(self) -> List[dict]:
        """汇总所有工具的 function-calling schema，供 LLM 选择。"""

        return [
            {"type": "function", "function": tool.schema()}
            for tool in self._tools.values()
        ]

    def execute(self, name: str, args: dict | None = None) -> ToolResult:
        """统一执行入口。

        职责：
        - 校验工具是否存在；
        - 捕获工具内部异常并转成 ToolResult 失败态（不让异常打断 Agent Loop）。
        """

        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(success=False, error=f"unknown tool: {name}")

        try:
            return tool.execute(**(args or {}))
        except Exception as exc:  # noqa: BLE001 —— 工具异常必须被吸收
            return ToolResult(
                success=False,
                error=f"{name} 执行失败: {exc}",
            )
