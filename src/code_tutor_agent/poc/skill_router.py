"""Skill 路由器 — 根据 topic 自动选择最合适的 skill。

核心功能：
    1. 扫描 skills/ 目录，自动发现所有 skill
    2. 根据 topic 关键词路由到对应 skill
    3. 支持手动指定 skill
    4. 支持 fallback 到默认 skill
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .skill_parser import Skill, load_skill


# 路由规则：topic 关键词 → skill 名称
# 匹配优先级：从上到下，先匹配先路由
ROUTING_RULES: list[tuple[list[str], str]] = [
    (["动态规划", "dp", "背包", "区间调度", "最长子序列", "状态转移"], "generate-dp"),
    (["数组", "哈希表", "双指针", "排序", "查找", "滑动窗口", "前缀和"], "generate-array"),
]


# 默认 skill（没有匹配时使用）
DEFAULT_SKILL = "generate-array"


class SkillRouter:
    """Skill 路由器。"""

    def __init__(self, skills_dir: Optional[Path] = None):
        self.skills_dir = skills_dir or (Path(__file__).resolve().parent / "skills")
        self._cache: dict[str, Skill] = {}

    def _discover(self) -> list[str]:
        """扫描 skills/ 目录，返回所有 skill 名称。"""
        if not self.skills_dir.exists():
            return []
        return sorted([
            d.name for d in self.skills_dir.iterdir()
            if d.is_dir() and (d / "SKILL.md").exists()
        ])

    def list_skills(self) -> list[dict]:
        """列出所有可用 skill。"""
        skills = []
        for name in self._discover():
            try:
                skill = self._get(name)
                skills.append({
                    "name": skill.name,
                    "description": skill.description,
                    "model": skill.model,
                    "schema_fields": list(skill.schema.get("properties", {}).keys()) if skill.schema else [],
                    "examples_count": len(skill.examples),
                })
            except Exception as e:
                skills.append({"name": name, "error": str(e)})
        return skills

    def _get(self, name: str) -> Skill:
        """加载 skill（带缓存）。"""
        if name not in self._cache:
            self._cache[name] = load_skill(name, self.skills_dir)
        return self._cache[name]

    def route(self, topic: str, difficulty: str = "easy") -> Skill:
        """根据 topic 路由到对应的 skill。

        匹配规则：
            1. 遍历 ROUTING_RULES，检查 topic 是否包含任意关键词
            2. 匹配成功 → 加载对应 skill
            3. 都不匹配 → 使用 DEFAULT_SKILL
        """
        topic_lower = topic.lower()

        for keywords, skill_name in ROUTING_RULES:
            if any(kw in topic_lower for kw in keywords):
                skill = self._get(skill_name)
                print(f"  [Router] 匹配关键词 '{[k for k in keywords if k in topic_lower][0]}' → {skill_name}")
                return skill

        print(f"  [Router] 无匹配关键词，使用默认 skill: {DEFAULT_SKILL}")
        return self._get(DEFAULT_SKILL)

    def invoke(self, topic: str, difficulty: str, llm_callable) -> dict:
        """一键路由 + 调用。

        Args:
            topic: 知识点
            difficulty: 难度
            llm_callable: LLM 调用函数，接收 prompt 返回文本

        Returns:
            校验后的 skill 输出 dict
        """
        skill = self.route(topic, difficulty)
        print(f"  [Router] 使用 skill: {skill.name} (model={skill.model}, temp={skill.temperature})")
        print(f"  [Router] 渲染 prompt 并调用 LLM...")
        result = skill.invoke(llm_callable, topic=topic, difficulty=difficulty)
        print(f"  [Router] LLM 调用完成，输出已通过 schema 校验")
        return result