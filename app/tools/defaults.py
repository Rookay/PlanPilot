# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：defaults.py
"""构建包含全部核心工具的默认 ToolRegistry。"""

from app.tools.event_tools import (
    CreateEventTool,
    CreateEventsTool,
    DeleteEventTool,
    DeleteEventsTool,
    GetEventTool,
    GetEventsTool,
    GetTripsTool,
    ReviseTripTool,
    UpdateEventTool,
)
from app.tools.external_tools import GetCurrentTimeTool, GetWeatherTool
from app.tools.map_tools import (
    EstimateTravelTimeTool,
    GeocodeAddressTool,
    SearchPlacesTool,
)
from app.tools.planning_tools import (
    AnalyzePlanRisksTool,
    ApplyPlanAlternativeTool,
    CheckConflictsTool,
    ComparePlanAlternativesTool,
    EvaluatePlanTool,
    GeneratePlanTool,
    LocalReplanTool,
    ReplanTool,
    UndoPlanTool,
)
from app.tools.preference_tools import GetPreferencesTool, UpdatePreferencesTool
from app.tools.registry import ToolRegistry
from app.tools.schedule_tools import (
    CreateScheduleTool,
    DeleteScheduleTool,
    FindFreeSlotsTool,
    GetSchedulesTool,
    UpdateScheduleTool,
)
from app.tools.skill_tools import SelectSkillTool
from app.tools.task_tools import (
    CompleteTaskTool,
    CreateTaskTool,
    DeleteTaskTool,
    GetTaskTool,
    GetTasksTool,
    UpdateTaskTool,
)


def build_default_registry(skill_registry=None) -> ToolRegistry:
    """构建默认工具集。

    ``skill_registry`` 用于装配 ``select_skill``：模型需要一个能列举并切换
    业务场景的入口。不传时该工具仍会注册，但执行时返回"场景注册表未配置"，
    以保持既有调用方（测试、脚本）的行为不变。
    """

    registry = ToolRegistry()

    for tool in [
        # Skill（理解阶段，始终对模型可见）
        SelectSkillTool(skill_registry),
        # Task
        GetTasksTool(),
        GetTaskTool(),
        CreateTaskTool(),
        UpdateTaskTool(),
        CompleteTaskTool(),
        DeleteTaskTool(),
        # Event
        GetEventsTool(),
        GetEventTool(),
        GetTripsTool(),
        CreateEventTool(),
        CreateEventsTool(),
        ReviseTripTool(),
        UpdateEventTool(),
        DeleteEventTool(),
        DeleteEventsTool(),
        # Schedule
        GetSchedulesTool(),
        FindFreeSlotsTool(),
        CreateScheduleTool(),
        UpdateScheduleTool(),
        DeleteScheduleTool(),
        # Planning
        GeneratePlanTool(),
        ComparePlanAlternativesTool(),
        ApplyPlanAlternativeTool(),
        AnalyzePlanRisksTool(),
        CheckConflictsTool(),
        EvaluatePlanTool(),
        ReplanTool(),
        LocalReplanTool(),
        UndoPlanTool(),
        # Preference
        GetPreferencesTool(),
        UpdatePreferencesTool(),
        # External
        GetCurrentTimeTool(),
        GetWeatherTool(),
        SearchPlacesTool(),
        GeocodeAddressTool(),
        EstimateTravelTimeTool(),
    ]:
        registry.register(tool)

    return registry
