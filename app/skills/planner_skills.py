# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：planner_skills.py
"""第一版 5 个技能定义与默认注册表构建。"""

from app.skills.base import Skill, SkillRegistry


def build_default_skills() -> SkillRegistry:
    registry = SkillRegistry(
        [
            Skill(
                name="task_understanding",
                description="把自然语言需求转换为结构化任务",
                keywords=["要做", "需要完成", "添加任务", "新建任务", "创建任务", "记得"],
                system_hint=(
                    "【任务理解】用户描述了一件要做的事。请抽取任务名称、预计时长、"
                    "优先级、截止时间等字段。只有外部时间已经确定且不能移动时才创建固定行程，"
                    "其余情况调用 create_task，让规划器生成任务安排。"
                ),
            ),
            Skill(
                name="schedule_planning",
                description="把任务 + 事件 + 约束转换为日程",
                keywords=["安排", "规划", "排期"],
                system_hint=(
                    "【日程规划】先 get_current_time 确定目标时间，再 get_tasks / get_events 了解现状，"
                    "如果用户说某个任务「急 / 尽快 / 优先」，先用 get_tasks 按关键词找到它，"
                    "再用 update_task 把 priority 改为 high，必要时设置 earliest_start 为当前时间；"
                    "然后调用 generate_plan 生成日程：用户指定了时间段（如「今天晚上」「明天下午」）时用 windows 参数，"
                    "要求「留空闲 / 不要连续」时用 gap_minutes 参数（通常 30 到 60 分钟）。"
                    "工具调用完成后必须停止继续调用工具，用自然语言总结结果。"
                ),
            ),
            Skill(
                name="replanning",
                description="在当前日程基础上动态调整",
                keywords=["重新安排", "调整", "推迟", "取消", "改到", "变动", "变化"],
                system_hint=(
                    "【重规划】先了解被影响的任务/事件（get_tasks / get_events / get_schedules），"
                    "做必要的修改（如 create_event / update_task），"
                    "再调用 replan 重新生成日程（会产生确认请求）。"
                ),
            ),
            Skill(
                name="weather_planning",
                description="结合天气调整日程",
                keywords=["天气", "下雨", "下雪", "高温", "降温", "户外"],
                system_hint=(
                    "【天气规划】先用 get_current_time 确定日期，再调用 get_weather 获取真实天气。"
                    "随后查询任务和日程；如果天气会影响户外安排，解释影响并在需要时调用 replan。"
                ),
            ),
            Skill(
                name="travel_planning",
                description="查询地点并把真实通勤耗时用于规划",
                keywords=["地点", "地址", "在哪", "路线", "通勤", "路上", "开车", "公交", "步行"],
                system_hint=(
                    "【地点与通勤】地点含糊时先调用 search_places；完整地址可调用 geocode_address。"
                    "有多个合理候选时先请用户选择，不要猜测。确定起终点坐标后调用 "
                    "estimate_travel_time，路线距离和耗时必须以工具结果为准。"
                    "安排多个连续地点时逐段验证；通勤时间不足时先调整可移动任务，"
                    "固定行程无法调整则明确报告差额，不要写入明知会迟到的时间。"
                ),
            ),
            Skill(
                name="daily_planning",
                description="安排某一天",
                keywords=["今天", "明天", "后天", "今天安排", "明天安排"],
                system_hint=(
                    "【每日规划】先 get_current_time 确定目标日期，"
                    "再查该日期的任务与事件，最后 generate_plan 并解释。"
                ),
            ),
            Skill(
                name="conflict_resolution",
                description="检测并解决时间冲突",
                keywords=["冲突", "撞了", "重叠"],
                system_hint=(
                    "【冲突解决】先 check_conflicts 找出冲突，"
                    "再针对每个冲突提出调整方案（移动/缩短/取消），需要时调用 replan。"
                ),
            ),
        ]
    )
    return registry
