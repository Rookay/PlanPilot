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
                    "优先级、截止时间等字段，然后调用 create_task 创建任务。"
                ),
            ),
            Skill(
                name="schedule_planning",
                description="把任务 + 事件 + 约束转换为日程",
                keywords=["安排", "规划", "排期"],
                system_hint=(
                    "【日程规划】先 get_current_time 确定目标时间，再 get_tasks / get_events 了解现状，"
                    "然后调用 generate_plan 生成日程：用户指定了时间段（如「今天晚上」「明天下午」）时用 windows 参数，"
                    "要求「留空闲 / 不要连续」时用 gap_minutes 参数。最后用自然语言解释结果。"
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
