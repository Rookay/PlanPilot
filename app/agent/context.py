# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：context.py
"""Context Builder：构造发送给 LLM 的上下文。

原则：不把整个数据库发给 LLM，只给一个紧凑快照，其余按需通过工具查询。
"""

from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.schedule import Schedule
from app.models.task import Task
from app.services import preference_service
from app.services import location_service
from app.utils import local_now, utc_offset_hours, utcnow

_SYSTEM_PROMPT = """你是 PlanPilot 的个人规划助手，帮助用户管理任务（Task）、固定事件（Event）和时间安排（Schedule）。

核心原则：
1. 你不直接计算排期，也不直接读写数据库；一切操作必须通过提供的工具完成。
2. 待办任务排期、冲突检测、空闲时间等确定性计算，必须调用 generate_plan / check_conflicts / find_free_slots 等工具，不要自己心算。
3. 生成日程会产生 proposed（待确认）状态，这是非破坏性的，可直接执行；删除任务/事件/日程等破坏性操作需要用户确认。
4. 同一个工具和同一组参数不要重复调用；一旦 generate_plan / replan / check_conflicts 等工具返回成功，必须用自然语言总结结果，不要继续循环调用工具。
5. 用简体中文、简洁地回复，说明你做了什么以及结果。
6. 用户询问天气时必须调用 get_weather，不得编造天气；外部工具失败时如实说明，并继续处理不依赖天气的部分。
7. 用户表达长期习惯时，先读取 get_preferences，再通过 update_preferences 提议修改，等待用户确认。
8. 用户询问地点、距离或通勤时间时，使用 search_places / geocode_address / estimate_travel_time 获取真实结果，不得自行估算；地点存在多个合理候选时先请用户确认。地图服务未配置或调用失败时如实说明，不影响其它规划。
9. 用户要求删除多个事件或清空某段行程时，先用 get_events 找出所有匹配项，再调用一次 delete_events；不要逐个调用 delete_event，也不要用普通文本自行询问确认，确认必须由工具生成。
10. 创建或更新任务/固定日程时，若地点与“常用地点”匹配，应传入 location_id；否则先用 search_places 或 geocode_address 定位。只有一个明确候选时，把名称、城市、经纬度写入 location、location_city、location_longitude、location_latitude，不能只写进描述或备注；有多个合理候选时先让用户选择。
11. 严格区分三类数据：外部时间已确定且不可移动的是 Event（固定日程）；尚未确定具体时刻、可由规划器移动的是 Task；Schedule 是系统为 Task 生成的任务安排，不能脱离 Task 创建。用户明确指定一组连续且不可拆开的多段活动时，使用 create_events 创建包含活动与赶路段的完整日程组；其它普通待办仍使用 Task / Schedule。
12. 一次请求中若有两个及以上连续的不同地点，写入安排前必须逐段调用 estimate_travel_time，并在前一项结束和后一项开始之间完整预留工具返回的通勤时间。固定时间无法满足时不要创建明知会迟到的安排，应指出差额并请用户调整；generate_plan 已内置同样的高德通勤约束。
13. 固定日程的时间段本身用于赶路（如“前往新街口”“返回学校”）时，创建或更新事件必须设置 event_type="travel"，并设置 travel_mode="walking" / "transit" / "taxi" / "driving"。步行、公交和自驾必须与 estimate_travel_time 查询时的 mode 一致；打车使用 travel_mode="taxi"，但路线查询使用 mode="driving"。此时 origin_location（及其城市、经纬度或常用地点 id）表示出发地，location 表示目的地，start_time 是出发时间，end_time 是最晚到达时间。已知出发地时必须填写；只有紧接上一事项出发且地点相同时才可省略，由系统继承上一地点。其它会议、用餐、游玩等使用 event_type="activity"。
14. 任何新任务、固定日程或任务安排都不能放在当前时间之前。用户给出的时间已经过去时，先说明并改用最近可行的未来时间；自动规划从当前时间开始，不得生成历史日程。
15. 用户要求长任务分段时，通过 update_task 设置 splittable=true，并合理设置 min_chunk_minutes、max_chunk_minutes；用户说明先后关系时写入 dependency_ids，不得仅记录在描述中。
16. 已锁定日程不可移动。用户要求调整某一天或某一周时，优先使用 replan_local，先预览并统一确认；用户要求撤销最近的日程修改时使用 undo_latest_plan。
17. 多地点连续日程不得调用 generate_plan，因为它只会安排数据库里的可调整任务。应先用 get_events 查询目标日期已有安排，再逐段搜索地点并估算路线；将活动与赶路组成连续时间线，最后只调用一次 create_events 供用户统一确认。地点搜索或路线失败时应明确缺失项，不得用无关任务安排冒充多段固定日程。
18. 用户要求调整刚才或已有的多段日程时，先调用 get_trips 定位日程组，再调用 get_events 获取事件 id，最后只调用一次 revise_trip 完成整组平移、批量修改或删除。不得逐条 update_event，也不得留下半段赶路；修改后的完整时间线必须重新通过地图校验并统一确认。
19. 用户询问某段日期中“哪天更合适”时，只读取该日期范围内的日程；同一路线只估算一次。多日天气必须通过 get_weather 的 start_date/end_date 一次查询完整范围，不得逐日调用；每个相关城市最多查询一次。取得足够数据后立即给出排序和理由，不要继续收集重复信息。
20. 对话历史只用于理解“刚才/那个安排”等指代，不代表当前数据库状态。用户可能在页面手动新增、修改或删除数据；凡是判断任务、固定日程或任务安排是否存在，必须以当前数据快照和 get_tasks / get_events / get_schedules 的最新结果为准，不得依据旧回复声称记录仍然存在。
21. 用户询问计划是否合理、是否来得及或有哪些风险时，先用 get_current_time 明确范围，再调用 analyze_plan_risks。风险分析只提供建议，不得未经确认直接修改日程；需要调整时使用 replan_local 预览并等待统一确认。

时区规则（非常重要）：
- 系统内部统一使用 UTC 存储时间，传给工具的时间必须是 UTC（ISO 8601，带 Z 后缀）。
- 用户所在时区是 {timezone}（UTC{offset_sign}{offset_abs}）。
- 用户说的「上午/下午/晚上/几点」都是本地时间，你必须先按用户时区理解，再转换为 UTC 传给工具。
- 例：用户在 UTC+8，说「下午 2 点」，应传 06:00Z。
"""


def build_system_prompt(timezone: str = "Asia/Shanghai") -> str:
    offset = utc_offset_hours(timezone)
    offset_sign = "+" if offset >= 0 else "-"
    offset_abs = abs(offset)
    return _SYSTEM_PROMPT.format(
        timezone=timezone,
        offset_sign=offset_sign,
        offset_abs=offset_abs,
    )


def build_world_summary(db: Session, timezone: str = "Asia/Shanghai", limit: int = 12) -> str:
    """构建当前世界的紧凑快照。"""

    now = utcnow()
    local = local_now(timezone)
    offset = utc_offset_hours(timezone)

    tasks = (
        db.query(Task)
        .filter(Task.status.in_(["pending", "in_progress"]))
        .order_by(Task.created_at.desc())
        .limit(limit)
        .all()
    )
    events = (
        db.query(Event)
        .filter(Event.end_time >= now)
        .order_by(Event.start_time)
        .limit(limit)
        .all()
    )
    schedules = (
        db.query(Schedule)
        .filter(
            Schedule.end_time >= now,
            Schedule.status.in_(["proposed", "accepted"]),
        )
        .order_by(Schedule.start_time)
        .limit(limit)
        .all()
    )

    lines = [
        f"当前时间：UTC {now.isoformat()} | 本地({timezone}) {local.isoformat()}（UTC{offset:+d}）"
    ]

    preferences = preference_service.get_preferences(db)
    lines.append(
        "规划偏好："
        f"工作 {preferences.workday_start}-{preferences.workday_end}，"
        f"午休 {preferences.lunch_start or '无'}-{preferences.lunch_end or '无'}，"
        f"默认间隔 {preferences.default_gap_minutes} 分钟，"
        f"策略 {preferences.planning_strategy}，"
        f"默认地点 {preferences.default_location or '未设置'}"
    )

    locations = location_service.list_locations(db)
    location_names = {location.id: location.label for location in locations}
    lines.append("\n常用地点：")
    if locations:
        for location in locations:
            marker = "（默认出发点）" if location.is_default else ""
            detail = location.address or location.city or "无地址说明"
            lines.append(
                f"- {location.label}{marker} | {detail} | "
                f"{location.longitude:.6f},{location.latitude:.6f}"
            )
    else:
        lines.append("- （无）")

    lines.append("\n待办任务：")
    if tasks:
        for task in tasks:
            deadline = task.deadline.isoformat() if task.deadline else "无"
            task_location = location_names.get(task.location_id) or task.location or "无"
            lines.append(
                f"- #{task.id} {task.title} | 时长 {task.estimated_duration} 分 | "
                f"优先级 {task.priority} | 截止 {deadline} | 地点 {task_location} | "
                f"拆分 {'是' if task.splittable else '否'} | 前置 {task.dependency_ids or '无'}"
            )
    else:
        lines.append("- （无）")

    lines.append("\n近期固定日程：")
    if events:
        for event in events:
            event_location = location_names.get(event.location_id) or event.location or "无"
            origin_location = (
                location_names.get(event.origin_location_id)
                or event.origin_location
                or "继承上一地点"
            )
            mode_names = {
                "walking": "步行",
                "driving": "自驾",
                "taxi": "打车",
                "transit": "公交/地铁",
            }
            event_type = (
                f"赶路({mode_names.get(event.travel_mode, '公交/地铁')})"
                if event.event_type == "travel"
                else "普通活动"
            )
            location_summary = (
                f"路线 {origin_location} -> {event_location}"
                if event.event_type == "travel"
                else f"地点 {event_location}"
            )
            lines.append(
                f"- #{event.id} {event.title} | {event.start_time} ~ {event.end_time} | "
                f"类型 {event_type} | {location_summary} | "
                f"行程组 {event.trip_id or '无'}({event.trip_title or '无'})"
            )
    else:
        lines.append("- （无）")

    lines.append("\n近期任务安排：")
    if schedules:
        for schedule in schedules:
            lines.append(
                f"- #{schedule.id} 任务#{schedule.task_id} | "
                f"{schedule.start_time} ~ {schedule.end_time} | {schedule.status} | "
                f"分段 {schedule.chunk_index} | {'已锁定' if schedule.is_locked else '未锁定'}"
            )
    else:
        lines.append("- （无）")

    return "\n".join(lines)
