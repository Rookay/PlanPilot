# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：registry.py
"""Tool Registry：工具注册、schema 汇总、统一执行入口。"""

import logging
from typing import Dict, List, Optional

import jsonschema
from jsonschema import Draft202012Validator
from sqlalchemy.orm import Session

from app.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

#: 一次最多回传几条参数错误，避免错误信息把上下文吃掉。
_MAX_REPORTED_ERRORS = 3

#: 校验器缓存，键为 (工具名, id(schema))，避免每轮重复编译。
_VALIDATORS: Dict[tuple, Optional[Draft202012Validator]] = {}


def _validator_for(tool: Tool) -> Optional[Draft202012Validator]:
    """取工具的 schema 校验器；schema 为空或不合法时返回 None（跳过校验）。"""

    key = (tool.name, id(tool.input_schema))
    if key in _VALIDATORS:
        return _VALIDATORS[key]

    validator: Optional[Draft202012Validator] = None
    if tool.input_schema:
        try:
            Draft202012Validator.check_schema(tool.input_schema)
            validator = Draft202012Validator(tool.input_schema)
        except jsonschema.SchemaError:
            logger.warning("tool %s 的 input_schema 不合法，跳过参数校验", tool.name)

    _VALIDATORS[key] = validator
    return validator


def _describe_error(exc: jsonschema.ValidationError) -> str:
    """把 jsonschema 的英文报错翻成模型能直接据以改正的中文。"""

    path = ".".join(str(p) for p in exc.absolute_path)
    where = f"参数 {path}" if path else "参数"

    if exc.validator == "additionalProperties":
        allowed = set((exc.schema or {}).get("properties", {}))
        extra = [k for k in exc.instance if k not in allowed]
        return f"不允许未声明的参数 {', '.join(repr(k) for k in extra) or '(未知)'}"

    if exc.validator == "required":
        missing = [r for r in (exc.validator_value or []) if r not in exc.instance]
        return f"缺少必填参数 {', '.join(missing) or '(未知)'}"

    if exc.validator == "type":
        actual = type(exc.instance).__name__
        return f"{where} 应为 {exc.validator_value}，实际是 {actual}"

    if exc.validator == "enum":
        return f"{where} 只能取 {exc.validator_value}，实际是 {exc.instance!r}"

    if exc.validator in {"minimum", "maximum"}:
        bound = exc.validator_value
        return f"{where} 不能{'小于' if exc.validator == 'minimum' else '大于'} {bound}"

    return f"{where} 不合法：{exc.message}"


def _validate_arguments(tool: Tool, args: dict) -> Optional[str]:
    """校验参数形状。返回错误描述；通过则返回 None。

    这里校验的是 ``input_schema``，也就是下发给模型的那份说明书——
    只有这样 ``additionalProperties: false`` 才真正生效，拼错的字段名
    不会再被 ``**kwargs`` 静默丢弃。
    """

    validator = _validator_for(tool)
    if validator is None:
        return None

    errors = sorted(validator.iter_errors(args), key=lambda e: list(e.absolute_path))
    if not errors:
        return None

    parts = [_describe_error(exc) for exc in errors[:_MAX_REPORTED_ERRORS]]
    if len(errors) > _MAX_REPORTED_ERRORS:
        parts.append(f"另有 {len(errors) - _MAX_REPORTED_ERRORS} 处错误")

    message = f"{tool.name} 参数不合法，未执行：{'；'.join(parts)}。"

    allowed = list((tool.input_schema or {}).get("properties", {}))
    if allowed:
        message += f"该工具接受的参数：{', '.join(allowed)}。"

    return message


def _coerce_args(name: str, args: dict | None) -> tuple[dict, Optional[str]]:
    """把 ``None`` 规整成空字典；非字典直接判错。"""

    if args is None:
        return {}, None
    if not isinstance(args, dict):
        return {}, (
            f"{name} 的参数必须是 JSON 对象，实际是 {type(args).__name__}"
        )
    return args, None


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
        """统一执行入口：校验工具、校验参数、执行、吸收异常。

        工具异常被吸收并转成失败 ``ToolResult``，但同时通过 ``logger.exception``
        保留完整 traceback，便于线上排查。参数不合 schema 时**不执行工具**，
        直接把字段级错误回传给模型，让它下一轮自己改正。
        """

        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(success=False, error=f"unknown tool: {name}")

        safe_args, args_error = _coerce_args(name, args)
        if args_error:
            return ToolResult(success=False, error=args_error)

        invalid = _validate_arguments(tool, safe_args)
        if invalid:
            logger.warning("tool %s 参数校验未通过: %s", name, invalid)
            return ToolResult(success=False, error=invalid)

        try:
            return tool.execute(db, **safe_args)
        except Exception as exc:  # noqa: BLE001 —— 工具异常必须被吸收
            logger.exception(
                "tool %s crashed during execute (args=%s)", name, safe_args
            )
            return ToolResult(
                success=False,
                error=f"{name} 执行失败: {exc}",
            )

    def preview(self, name: str, args: dict | None, db: Session) -> ToolResult:
        """生成 confirm 级别工具的预览（不做修改）。

        与 ``execute`` 行为一致：先校验参数，再吸收异常但保留 traceback。
        参数不合 schema 时不生成预览——预览出来的东西本来就执行不了。
        """

        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(success=False, error=f"unknown tool: {name}")

        safe_args, args_error = _coerce_args(name, args)
        if args_error:
            return ToolResult(success=False, error=args_error)

        invalid = _validate_arguments(tool, safe_args)
        if invalid:
            logger.warning("tool %s 参数校验未通过（preview）: %s", name, invalid)
            return ToolResult(success=False, error=invalid)

        try:
            return tool.preview(db, **safe_args)
        except Exception as exc:  # noqa: BLE001 —— 预览失败不能让 Agent 崩
            logger.exception(
                "tool %s crashed during preview (args=%s)", name, safe_args
            )
            return ToolResult(
                success=False,
                error=f"{name} 预览失败: {exc}",
            )
