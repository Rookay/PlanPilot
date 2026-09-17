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

    def matches(self, message: str) -> bool:
        return any(keyword in message for keyword in self.keywords)


class SkillRegistry:
    def __init__(self, skills: Optional[List[Skill]] = None):
        self._skills = list(skills or [])

    def register(self, skill: Skill) -> None:
        self._skills.append(skill)

    def select(self, message: str) -> Optional[Skill]:
        """优先选择命中词更多、更具体的技能。"""

        ranked = []
        for index, skill in enumerate(self._skills):
            matched = [keyword for keyword in skill.keywords if keyword in message]
            if matched:
                ranked.append((sum(map(len, matched)), len(matched), -index, skill))
        return max(ranked, default=(0, 0, 0, None), key=lambda item: item[:3])[3]
