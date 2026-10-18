# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：registry.py
"""Tool Registry：工具注册、schema 汇总、统一执行入口。"""

import logging
from typing import Dict, List

from sqlalchemy.orm import Session

from app.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)


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

    def execute(self, name: str, args: dict | None, db: Session) -> ToolResult:
        """统一执行入口：校验工具、执行、吸收异常。

        工具异常被吸收并转成失败 ``ToolResult``，但同时通过 ``logger.exception``
        保留完整 traceback，便于线上排查。
        """

        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(success=False, error=f"unknown tool: {name}")

        try:
            return tool.execute(db, **(args or {}))
        except Exception as exc:  # noqa: BLE001 —— 工具异常必须被吸收
            logger.exception(
                "tool %s crashed during execute (args=%s)", name, args or {}
            )
            return ToolResult(
                success=False,
                error=f"{name} 执行失败: {exc}",
            )

    def preview(self, name: str, args: dict | None, db: Session) -> ToolResult:
        """生成 confirm 级别工具的预览（不做修改）。

        与 ``execute`` 行为一致：吸收异常但保留 traceback。
        """

        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(success=False, error=f"unknown tool: {name}")

        try:
            return tool.preview(db, **(args or {}))
        except Exception as exc:  # noqa: BLE001 —— 预览失败不能让 Agent 崩
            logger.exception(
                "tool %s crashed during preview (args=%s)", name, args or {}
            )
            return ToolResult(
                success=False,
                error=f"{name} 预览失败: {exc}",
            )
