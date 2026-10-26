# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：base.py
"""Skill 基类与按关键词相关度选择的提示词档案注册表。"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Skill:
    name: str
    description: str
    keywords: List[str] = field(default_factory=list)
    system_hint: str = ""
    allowed_tools: Optional[List[str]] = None
    priority: int = 0

    def matches(self, message: str) -> bool:
        return any(keyword in message for keyword in self.keywords)


class SkillRegistry:
    def __init__(self, skills: Optional[List[Skill]] = None):
        self._skills = list(skills or [])

    def register(self, skill: Skill) -> None:
        self._skills.append(skill)

    def all(self) -> List[Skill]:
        """返回全部已注册场景，供 ``select_skill`` 工具向模型展示清单。"""

        return list(self._skills)

    def names(self) -> List[str]:
        return [skill.name for skill in self._skills]

    def get(self, name: str) -> Optional[Skill]:
        """按标识查找场景；模型自选场景时以 name 为准，不看关键词。"""

        for skill in self._skills:
            if skill.name == name:
                return skill
        return None

    def select(self, message: str) -> Optional[Skill]:
        """优先选择命中词更多、更具体的技能。"""

        ranked = []
        for index, skill in enumerate(self._skills):
            matched = [keyword for keyword in skill.keywords if keyword in message]
            if matched:
                ranked.append(
                    (skill.priority, sum(map(len, matched)), len(matched), -index, skill)
                )
        return max(
            ranked,
            default=(0, 0, 0, 0, None),
            key=lambda item: item[:4],
        )[4]
