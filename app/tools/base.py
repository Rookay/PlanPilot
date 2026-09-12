# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：base.py
"""Tool 基类与通用结果类型。

Tool 是原子、确定性的能力单元，内部不含 LLM，不引入随机性。
所有工具通过统一的 :class:`ToolResult` 返回结果，便于 Agent Loop 观察与恢复。
"""

from abc import ABC, abstractmethod
from typing import Any, Literal, Optional

from pydantic import BaseModel
from sqlalchemy.orm import Session

ConfirmationLevel = Literal["none", "auto", "confirm"]


class ToolResult(BaseModel):
    """工具执行结果，所有工具统一返回。"""

    success: bool
    data: Any = None
    error: Optional[str] = None
    summary: Optional[str] = None
    #: 当工具需要用户确认时置为 True，Agent Loop 据此暂停并询问用户。
    needs_confirmation: bool = False


class Tool(ABC):
    """工具基类。

    子类需定义 name / description / input_schema / output_schema / confirmation，
    并实现 :meth:`execute`。

    - ``execute`` 不向外抛异常，错误统一封装进 ToolResult；
    - ``preview`` 用于 confirm 级别工具：只描述将要做什么、不做任何修改。
    """

    name: str = ""
    description: str = ""
    input_schema: dict = {}
    output_schema: dict = {}
    confirmation: ConfirmationLevel = "none"

    @abstractmethod
    def execute(self, db: Session, **kwargs: Any) -> ToolResult:
        ...

    def preview(self, db: Session, **kwargs: Any) -> ToolResult:
        """默认预览：只提示即将执行的工具名，不做任何副作用。"""

        return ToolResult(
            success=True,
            needs_confirmation=True,
            summary=f"即将执行 {self.name}，参数：{kwargs or {}}",
        )

    def schema(self) -> dict:
        """返回 OpenAI 风格 function-calling 描述，供 LLM 自动选择与传参。"""

        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema,
        }
