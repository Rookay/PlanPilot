# -*- coding: UTF-8 -*-
"""日程业务逻辑（兼容门面）。

实现已按职责拆分到 :mod:`app.services.schedule` 包：

- ``core``         规划基础设施与引擎调用
- ``comparison``   方案对比的数据结构构造
- ``plans``        计划生成、预览与试算
- ``alternatives`` 多方案对比
- ``local_replan`` 局部重排
- ``risks``        范围风险分析
- ``audit``        执行后审计
- ``crud``         日程增删改查与冲突检查
- ``versions``     计划版本与撤销

保留本模块是为了不改动既有调用点（API / tools / agent 共 37 处引用）。
"""

from app.services.schedule.plans import (
    generate_schedules,
    preview_plan,
    replan_preview,
    evaluate_plan_dry_run,
)
from app.services.schedule.alternatives import (
    generate_plan_alternatives,
    apply_plan_alternative,
)
from app.services.schedule.local_replan import (
    local_replan_preview,
    apply_local_replan,
)
from app.services.schedule.risks import (
    analyze_plan_risks,
)
from app.services.schedule.audit import (
    audit_current_plan,
    audit_applied_changes,
)
from app.services.schedule.crud import (
    list_schedules,
    create_schedule,
    accept_all_schedules,
    clear_schedules,
    update_schedule,
    delete_schedule,
    delete_task_schedules,
    find_free_slots,
    preview_schedule_update,
    check_conflicts,
)
from app.services.schedule.versions import (
    list_plan_versions,
    undo_latest_plan,
)

__all__ = [
    "generate_schedules",
    "preview_plan",
    "replan_preview",
    "evaluate_plan_dry_run",
    "generate_plan_alternatives",
    "apply_plan_alternative",
    "local_replan_preview",
    "apply_local_replan",
    "analyze_plan_risks",
    "audit_current_plan",
    "audit_applied_changes",
    "list_schedules",
    "create_schedule",
    "accept_all_schedules",
    "clear_schedules",
    "update_schedule",
    "delete_schedule",
    "delete_task_schedules",
    "find_free_slots",
    "preview_schedule_update",
    "check_conflicts",
    "list_plan_versions",
    "undo_latest_plan",
]
