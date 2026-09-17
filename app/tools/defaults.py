# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：defaults.py
"""构建包含全部核心工具的默认 ToolRegistry。"""

from app.tools.event_tools import (
    CreateEventTool,
    DeleteEventTool,
    DeleteEventsTool,
    GetEventTool,
    GetEventsTool,
    UpdateEventTool,
)
from app.tools.external_tools import GetCurrentTimeTool, GetWeatherTool
from app.tools.map_tools import (
    EstimateTravelTimeTool,
    GeocodeAddressTool,
    SearchPlacesTool,
)
from app.tools.planning_tools import (
    CheckConflictsTool,
    EvaluatePlanTool,
    GeneratePlanTool,
    ReplanTool,
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
from app.tools.task_tools import (
    CompleteTaskTool,
    CreateTaskTool,
    DeleteTaskTool,
    GetTaskTool,
    GetTasksTool,
    UpdateTaskTool,
)


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()

    for tool in [
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
        CreateEventTool(),
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
        CheckConflictsTool(),
        EvaluatePlanTool(),
        ReplanTool(),
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
