# -*- coding: UTF-8 -*-
"""Tool Registry 异常吸收的可观测性测试。

``ToolRegistry.execute`` / ``ToolRegistry.preview`` 必须在工具抛异常时不向外抛，
但同时要把完整 traceback 写进日志。这是排查线上崩溃的唯一入口。
"""

from unittest import TestCase

from app.tools.base import Tool, ToolResult
from app.tools.registry import ToolRegistry


class _BoomTool(Tool):
    """总是抛 RuntimeError 的测试工具。"""

    name = "boom"
    description = "故意抛异常的工具"
    input_schema: dict = {}
    output_schema: dict = {}
    confirmation = "none"

    def __init__(self, *, also_break_preview: bool = True):
        self._also_break_preview = also_break_preview

    def execute(self, db, **kwargs) -> ToolResult:
        raise RuntimeError("boom from execute")

    def preview(self, db, **kwargs) -> ToolResult:
        if self._also_break_preview:
            raise RuntimeError("boom from preview")
        return ToolResult(success=True, summary="ok")


class ToolRegistryErrorAbsorptionTests(TestCase):
    def test_execute_returns_failure_and_logs_traceback(self):
        registry = ToolRegistry()
        registry.register(_BoomTool(also_break_preview=False))

        with self.assertLogs("app.tools.registry", level="ERROR") as captured:
            result = registry.execute("boom", {"k": "v"}, db=None)

        self.assertFalse(result.success)
        self.assertIn("boom 执行失败", result.error or "")
        self.assertIn("boom from execute", result.error or "")
        joined = "\n".join(captured.output)
        # 必须看到 logger.exception 写出的 traceback 关键行
        self.assertIn("Traceback", joined)
        self.assertIn("boom from execute", joined)
        # 也应把工具名暴露出来便于 grep
        self.assertIn("boom", joined)

    def test_preview_returns_failure_and_logs_traceback(self):
        registry = ToolRegistry()
        registry.register(_BoomTool())

        with self.assertLogs("app.tools.registry", level="ERROR") as captured:
            result = registry.preview("boom", {}, db=None)

        self.assertFalse(result.success)
        self.assertIn("boom 预览失败", result.error or "")
        self.assertIn("boom from preview", result.error or "")
        joined = "\n".join(captured.output)
        self.assertIn("Traceback", joined)
        self.assertIn("boom from preview", joined)

    def test_unknown_tool_still_returns_failure_without_logs(self):
        """未知工具走另一条早返回路径——既不报错也不应堆日志。"""

        registry = ToolRegistry()
        # 故意不 register
        result = registry.execute("nope", {}, db=None)
        self.assertFalse(result.success)
        self.assertIn("unknown tool: nope", result.error or "")

    def test_execute_does_not_propagate_exception(self):
        """回归保护：即使工具炸了，调用方也不该拿到 RuntimeError。"""

        registry = ToolRegistry()
        registry.register(_BoomTool())

        # 不应向外抛—— assertLogs 抓住 logger.exception
        with self.assertLogs("app.tools.registry", level="ERROR"):
            try:
                registry.execute("boom", {}, db=None)
            except Exception as exc:  # noqa: BLE001
                self.fail(
                    f"工具异常不应透传到调用方，但收到 {type(exc).__name__}: {exc}"
                )

    def test_existing_successful_tool_still_works(self):
        """保证没有改动 happy-path 行为。"""

        class _OkTool(Tool):
            name = "ok"
            description = ""
            input_schema: dict = {}
            output_schema: dict = {}
            confirmation = "none"

            def execute(self, db, **kwargs) -> ToolResult:
                return ToolResult(success=True, data={"x": 1}, summary="ok")

        registry = ToolRegistry()
        registry.register(_OkTool())

        # 成功路径不应写 ERROR 日志
        result = registry.execute("ok", {}, db=None)
        self.assertTrue(result.success)
        self.assertEqual(result.data, {"x": 1})
