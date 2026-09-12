# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：base.py
"""Skill 基类与注册表。

第一版：Skill 是「意图 → 推荐工具流程」的提示词档案（prompt profile），
通过关键词匹配被选中，并注入到 system prompt 中引导 LLM。
后续可升级为带确定性 run() 的完整工作流。
"""

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
        """按关键词匹配返回第一个命中的技能。"""

        for skill in self._skills:
            if skill.matches(message):
                return skill
        return None
