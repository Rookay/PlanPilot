# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：test_tool_schema_validation.py
"""工具参数形状校验：让 ``additionalProperties: false`` 真正生效。

``input_schema`` 原先只作为说明书下发给模型，服务端没有任何地方执行校验，
而工具用 ``**kwargs`` 接收参数，于是字段名拼错会被静默丢弃、缺的键走默认值。
典型的坏结果是：模型传 ``duration=90``、schema 要求 ``estimated_duration``，
用户以为改成了 90 分钟，实际按默认 60 分钟执行并返回成功。

这里锁住 ``ToolRegistry`` 的新行为：执行前先按 schema 校验，不通过就不执行工具，
把字段级错误回传给模型。
"""

from unittest import TestCase

from app.models.task import Task
from app.tools.base import Tool, ToolResult
from app.tools.defaults import build_default_registry
from tests.helpers import make_session


class ArgumentSchemaTests(TestCase):
    """execute 入口的校验行为。"""

    def setUp(self):
        self.db = make_session()
        self.registry = build_default_registry()

    def test_extra_parameter_is_rejected(self):
        result = self.registry.execute(
            "create_task",
            {"title": "写论文", "duration": 90},
            self.db,
        )

        self.assertFalse(result.success)
        self.assertIn("不允许未声明的参数", result.error)
        self.assertIn("duration", result.error)
        # 报错要告诉模型正确写法，否则它下一轮还是会猜
        self.assertIn("estimated_duration", result.error)

    def test_extra_parameter_does_not_touch_database(self):
        self.registry.execute(
            "create_task",
            {"title": "写论文", "duration": 90},
            self.db,
        )

        # 关键：没有被当成合法的宽松调用执行掉
        self.assertEqual(self.db.query(Task).count(), 0)

    def test_missing_required_parameter_is_reported(self):
        result = self.registry.execute("delete_task", {}, self.db)

        self.assertFalse(result.success)
        self.assertIn("缺少必填参数", result.error)
        self.assertIn("task_id", result.error)

    def test_wrong_type_is_reported(self):
        result = self.registry.execute("delete_task", {"task_id": "七"}, self.db)

        self.assertFalse(result.success)
        self.assertIn("应为 integer", result.error)
        self.assertIn("str", result.error)

    def test_enum_violation_lists_choices(self):
        result = self.registry.execute(
            "create_task",
            {"title": "写论文", "priority": "urgent"},
            self.db,
        )

        self.assertFalse(result.success)
        self.assertIn("只能取", result.error)
        self.assertIn("high", result.error)

    def test_none_arguments_are_fine_for_optional_only_tool(self):
        result = self.registry.execute("get_tasks", None, self.db)

        # 全部可选参数的查询工具，空参数是合法的
        self.assertTrue(result.success)

    def test_non_dict_arguments_are_rejected(self):
        result = self.registry.execute("get_tasks", ["论文"], self.db)

        self.assertFalse(result.success)
        self.assertIn("必须是 JSON 对象", result.error)

    def test_valid_arguments_still_execute(self):
        result = self.registry.execute(
            "create_task",
            {"title": "写论文", "estimated_duration": 90},
            self.db,
        )

        self.assertTrue(result.success)
        self.assertEqual(self.db.query(Task).count(), 1)
        self.assertEqual(self.db.query(Task).first().estimated_duration, 90)


class PreviewSchemaTests(TestCase):
    """preview 与 execute 共用同一套校验。"""

    def setUp(self):
        self.db = make_session()
        self.registry = build_default_registry()

    def test_preview_rejects_invalid_arguments(self):
        result = self.registry.preview("delete_task", {"taskId": 1}, self.db)

        self.assertFalse(result.success)
        self.assertFalse(result.needs_confirmation)
        self.assertIn("taskId", result.error)

    def test_preview_accepts_valid_arguments(self):
        task = self.registry.execute("create_task", {"title": "写论文"}, self.db)
        task_id = task.data["id"]

        result = self.registry.preview("delete_task", {"task_id": task_id}, self.db)

        self.assertTrue(result.success)
        self.assertTrue(result.needs_confirmation)


class _BrokenSchemaTool(Tool):
    name = "broken_schema_tool"
    description = "schema 本身写错时不能把整个请求带崩"
    input_schema = {"type": "object", "properties": "这里本该是对象"}
    confirmation = "none"

    def execute(self, db, **kwargs):
        return ToolResult(success=True, summary="executed")


class BrokenSchemaTests(TestCase):
    def test_invalid_schema_skips_validation_instead_of_crashing(self):
        from app.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register(_BrokenSchemaTool())

        result = registry.execute("broken_schema_tool", {"whatever": 1}, make_session())

        # schema 不合法只跳过校验并记 warning，不阻断执行
        self.assertTrue(result.success)
