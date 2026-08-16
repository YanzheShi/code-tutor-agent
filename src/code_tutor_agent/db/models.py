"""Pydantic models mapping 1:1 to SQLite tables (problems, submissions, profiles)."""
from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import BaseModel, Field


class DBProblem(BaseModel):
    """Maps 1:1 to the ``problems`` table in SQLite."""
    id: int = Field(description="Primary key")
    title: str = Field(description="题目标题")
    topic: str = Field(description="知识点标签")
    difficulty: str = Field(description="easy / medium / hard")
    description: str = Field(description="题目描述")
    optimal_solution: str = Field(default="", description="最优解代码")
    brute_solution: str = Field(default="", description="暴力解代码")
    function_signature: str = Field(default="", description="方法签名")
    time_complexity: str = Field(default="", description="时间复杂度")
    space_complexity: str = Field(default="", description="空间复杂度")
    novelty_score: float = Field(default=7.0, ge=0.0, le=10.0, description="新颖度评分")
    starter_code: str = Field(default="", description="模板代码")
    source: str = Field(default="generated", description="题目来源")
    source_url: str = Field(default="", description="来源 URL")
    created_at: str = Field(default="", description="创建时间")
    test_cases_json: str = Field(default="[]", description="全量测试用例 JSON")
    visible_test_cases_json: str = Field(default="[]", description="可见测试用例 JSON")
    adversarial_spec_json: str = Field(default="", description="对抗规格 JSON")
    alternative_solutions: str = Field(default="[]", description="备选解法 JSON")
    constraints_json: str = Field(default="[]", description="约束条件 JSON")

    @property
    def test_cases(self) -> list[dict]:
        return json.loads(self.test_cases_json) if self.test_cases_json else []

    @property
    def visible_test_cases(self) -> list[dict]:
        return json.loads(self.visible_test_cases_json) if self.visible_test_cases_json else []

    @property
    def adversarial_spec(self) -> Optional[dict]:
        return json.loads(self.adversarial_spec_json) if self.adversarial_spec_json else None

    @property
    def alternative_solutions_list(self) -> list[str]:
        return json.loads(self.alternative_solutions) if self.alternative_solutions else []

    @property
    def constraints(self) -> list[str]:
        return json.loads(self.constraints_json) if self.constraints_json else []

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            val = getattr(self, key)
            return val if val is not None else default
        except AttributeError:
            return default


class DBSubmission(BaseModel):
    """Maps 1:1 to the ``submissions`` table in SQLite."""
    id: int = Field(description="Primary key")
    problem_id: int = Field(description="FK → problems.id")
    session_id: str = Field(default="", description="所属会话 ID")
    student_code: str = Field(description="用户提交的代码")
    status: str = Field(description="状态（judged / ...）")
    verdict: str = Field(default="", description="最终判题结论")
    judge_results: str = Field(default="[]", description="判题结果 JSON")
    feedback: Optional[str] = Field(default=None, description="导师反馈")
    created_at: str = Field(default="", description="提交时间")

    @property
    def timestamp(self) -> str:
        return self.created_at

    @property
    def judge_results_list(self) -> list[dict]:
        return json.loads(self.judge_results) if self.judge_results else []

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "code": self.student_code,
            "status": self.status,
            "verdict": self.verdict,
            "judge_results": self.judge_results_list,
            "feedback": self.feedback,
            "timestamp": self.created_at,
            "created_at": self.created_at,
        }

    def get(self, key: str, default: Any = None) -> Any:
        try:
            val = getattr(self, key)
            return val if val is not None else default
        except AttributeError:
            return default


class DBProfile(BaseModel):
    """User profile — 5-dimension skill vector.

    Stored as JSON in the profiles table's profile_json column.
    One row per simulated user (single-user mode).
    """
    proficiency: float = Field(default=0.5, ge=0.0, le=1.0, description="熟练度")
    stability: float = Field(default=0.5, ge=0.0, le=1.0, description="稳定性")
    forget_days: int = Field(default=0, ge=0, description="距离上次做题天数")
    common_errors: list[str] = Field(default_factory=list, description="常见错误")
    attempts: int = Field(default=0, ge=0, description="做过多少题")
    error_modes: dict = Field(default_factory=dict, description="错误模式画像(6维结构化)")