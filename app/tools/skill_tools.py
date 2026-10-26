# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：skill_tools.py
"""场景切换工具：允许模型纠正关键词路由的错误匹配。

关键词路由（``SkillRegistry.select``）只做子串匹配，遇到同义表达、否定句或
多个意图命中时容易选错。本工具把"选场景"从纯规则改成"规则先验 + 模型可修正"：

- 规则先给出初值，减少一次模型决策；
- 模型若发现当前场景与用户真实意图不符，可调用 ``select_skill`` 切换；
- 切换只影响本轮 run 的 system_hint 与工具可见集，不写数据库、不可回滚。
"""

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.skills.base import SkillRegistry
from app.tools.base import Tool, ToolResult


class SelectSkillTool(Tool):
    """按 name 切换当前对话的业务场景指引。"""

    name = "select_skill"
    confirmation = "none"

    def __init__(self, skill_registry: Optional[SkillRegistry] = None):
        self.skill_registry = skill_registry
        self.description = self._build_description()
        self.input_schema = self._build_schema()

    def _build_description(self) -> str:
        base = (
            "切换当前对话的业务场景指引。系统会先按关键词自动匹配场景，"
            "如果匹配结果与用户真实意图不符（例如同义说法、否定句、一句话包含多个意图），"
            "调用本工具切换到更合适的场景。切换后本轮后续步骤会使用新场景的操作指引。"
        )
        if self.skill_registry is None:
            return base
        catalog = "；".join(
            f"{skill.name}（{_short(skill.description)}）"
            for skill in self.skill_registry.all()
        )
        return f"{base}可选场景：{catalog}。"

    def _build_schema(self) -> dict:
        names = self.skill_registry.names() if self.skill_registry is not None else []
        schema: dict = {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "目标场景标识；留空可列出全部可选场景",
                },
                "reason": {
                    "type": "string",
                    "description": "为什么该场景更贴合用户意图（可选，用于轨迹排查）",
                },
            },
            "required": ["name"],
        }
        if names:
            schema["properties"]["name"]["enum"] = names
        return schema

    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        registry = self.skill_registry
        if registry is None:
            return ToolResult(success=False, error="场景注册表未配置，无法切换场景")

        if not registry.all():
            return ToolResult(success=False, error="当前没有可用的业务场景")

        requested = str(kwargs.get("name") or "").strip()
        if not requested:
            return ToolResult(
                success=True,
                summary="可用场景：" + "、".join(registry.names()),
                data={
                    "skills": [
                        {"name": item.name, "description": item.description}
                        for item in registry.all()
                    ]
                },
            )

        skill = registry.get(requested)
        if skill is None:
            return ToolResult(
                success=False,
                error=(
                    f"未知场景：{requested}；可选场景："
                    + "、".join(registry.names())
                ),
            )

        # system_hint 不放进 data：工具结果会被 _compact_tool_payload 压缩，
        # 超长提示词可能被截断。Loop 侧按 name 从注册表取完整定义。
        return ToolResult(
            success=True,
            summary=f"已切换到「{skill.description}」场景",
            data={
                "name": skill.name,
                "description": skill.description,
                "allowed_tools": skill.allowed_tools,
                "reason": kwargs.get("reason") or "",
            },
        )


def _short(text: str, limit: int = 14) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"
