"""SKILL.md 解析器 — 解析 skills.sh 格式的 SKILL.md 文件。

从 YAML frontmatter 中提取元数据、schema、examples。
从 <prompt> 标签中提取 prompt 模板。
不依赖项目现有代码，纯 Python 标准库 + yaml。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import yaml


class Skill:
    """加载后的 skill 实例。

    核心方法：
        render_prompt(**kwargs) -> str     # 渲染 prompt 模板
        invoke(llm, **kwargs) -> dict      # 调 LLM + 校验输出
        validate(output: dict) -> bool     # 按 schema 校验
    """

    def __init__(
        self,
        name: str,
        description: str,
        metadata: dict,
        prompt_template: str,
        schema: Optional[dict],
        examples: list[dict],
    ):
        self.name = name
        self.description = description
        self.metadata = metadata
        self.prompt_template = prompt_template
        self.schema = schema or {}
        self.examples = examples
        self.model = metadata.get("model", "sensenova-deepseek")
        self.temperature = metadata.get("temperature", 0.7)

    def render_prompt(self, **kwargs) -> str:
        """用传入的参数渲染 prompt 模板。"""
        return self.prompt_template.format(**kwargs)

    def validate(self, output: dict) -> tuple[bool, list[str]]:
        """按 schema 校验 LLM 输出。"""
        errors = []
        if not self.schema:
            return True, errors
        required = self.schema.get("required", [])
        props = self.schema.get("properties", {})

        for field in required:
            if field not in output or output.get(field) is None:
                errors.append(f"缺少必填字段: {field}")
            else:
                val = output[field]
                prop = props.get(field, {})
                prop_type = prop.get("type", "")
                if prop_type == "string" and not isinstance(val, str):
                    errors.append(f"字段 {field} 应为 string，实际为 {type(val).__name__}")
                elif prop_type == "array" and not isinstance(val, list):
                    errors.append(f"字段 {field} 应为 array，实际为 {type(val).__name__}")

        return len(errors) == 0, errors

    def invoke(self, llm_callable, **kwargs) -> dict:
        """渲染 prompt → 调 LLM → 解析输出 → 校验 → 返回。

        llm_callable 是一个可调用对象，接收 prompt 字符串，返回响应文本。
        支持 LangChain 的 AIMessage 返回类型（自动提取 .content）。
        """
        prompt = self.render_prompt(**kwargs)
        raw_response = llm_callable(prompt)
        # 兼容 LangChain 的 AIMessage 对象
        if hasattr(raw_response, "content"):
            raw_response = raw_response.content
        if not isinstance(raw_response, str):
            raw_response = str(raw_response)
        result = self._parse_json(raw_response)
        is_valid, errors = self.validate(result)
        if not is_valid:
            raise ValueError(
                f"Skill '{self.name}' 输出校验失败 ({len(errors)} 个错误):\n"
                + "\n".join(f"  - {e}" for e in errors)
            )
        return result

    @staticmethod
    def _parse_json(text: str) -> dict:
        """从 LLM 响应中提取 JSON，支持 markdown 代码块包裹。"""
        # 尝试从 ```json ... ``` 中提取
        m = re.search(r"```(?:json)\s*\n(.*?)```", text, re.DOTALL)
        if m:
            return json.loads(m.group(1))
        # 尝试从 ``` ... ``` 中提取
        m = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
        if m:
            return json.loads(m.group(1))
        # 直接解析整个文本
        return json.loads(text)

    def __repr__(self) -> str:
        return f"Skill(name='{self.name}', model={self.model}, temp={self.temperature})"


def parse_skill_md(filepath: Path) -> Skill:
    """解析 SKILL.md 文件，返回 Skill 实例。

    解析策略：
        1. 提取 --- 之间的 YAML frontmatter → metadata
        2. 提取 <prompt> 标签之间的内容 → prompt 模板
        3. 从 metadata 中取 output_schema 和 examples
    """
    raw = filepath.read_text(encoding="utf-8")

    # 1. 提取 frontmatter
    fm_match = re.match(r"^---\r?\n(.*?)\r?\n---", raw, re.DOTALL)
    if not fm_match:
        raise ValueError(f"SKILL.md 缺少 frontmatter: {filepath}")

    metadata = yaml.safe_load(fm_match.group(1)) or {}
    body = raw[fm_match.end():].strip()

    name = metadata.get("name", filepath.parent.name)
    description = metadata.get("description", "")

    # 2. 提取 prompt 模板（<prompt> 标签内的内容）
    prompt_match = re.search(r"<prompt>\s*\n(.*?)</prompt>", body, re.DOTALL)
    prompt_template = prompt_match.group(1).strip() if prompt_match else body

    # 3. 从 frontmatter 中取结构化字段
    schema = metadata.get("output_schema")
    examples = metadata.get("examples", [])

    return Skill(
        name=name,
        description=description,
        metadata=metadata,
        prompt_template=prompt_template,
        schema=schema,
        examples=examples,
    )


def load_skill(name: str, skills_dir: Optional[Path] = None) -> Skill:
    """按技能名加载 skill。

    Args:
        name: 技能名，如 "generate-array"
        skills_dir: skills 根目录，默认 ../poc/skills/{name}
    """
    if skills_dir is None:
        skills_dir = Path(__file__).resolve().parent / "skills"
    skill_path = skills_dir / name / "SKILL.md"
    if not skill_path.exists():
        available = [d.name for d in skills_dir.iterdir() if d.is_dir() and (d / "SKILL.md").exists()]
        raise FileNotFoundError(
            f"Skill '{name}' 不存在于 {skills_dir}\n"
            f"可用的 skill: {', '.join(available)}"
        )
    return parse_skill_md(skill_path)