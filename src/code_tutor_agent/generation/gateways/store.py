"""StoreGateway — 落库 / 静态题库 / 历史未 AC 题薄封装（设计 §8）。"""

from __future__ import annotations

import logging

from code_tutor_agent.generation.state import ProblemDraft

logger = logging.getLogger(__name__)


def draft_to_problem_dict(draft: ProblemDraft) -> dict:
    """ProblemDraft → save_problem 可接受的 dict。"""
    return {
        "title": draft.title,
        "topic": draft.topic,
        "difficulty": draft.difficulty,
        "description": draft.description,
        "starter_code": draft.starter_code,
        "optimal_solution": draft.optimal_solution,
        "brute_solution": draft.brute_solution,
        "examples": draft.examples,
        "constraints": draft.constraints,
        "tags": draft.tags,
        "function_signature": draft.function_signature,
        "test_cases": draft.test_cases,
        "novelty_score": 9.0 if draft.from_leetcode else 7.0,
        "source": "leetcode" if draft.from_leetcode else "generated",
        "source_url": (
            f"https://leetcode.cn/problems/{draft.source_slug}/"
            if draft.from_leetcode else ""
        ),
    }


def flat_to_draft(flat: dict | None, topic: str, difficulty: str) -> ProblemDraft | None:
    """静态题库 / DB 的扁平 dict → ProblemDraft；dict 缺失或不可用返回 None。"""
    if not flat or not flat.get("title"):
        return None
    return ProblemDraft(
        topic=(flat.get("topic") or topic),
        difficulty=(flat.get("difficulty") or difficulty),
        title=flat["title"],
        description=flat.get("description", ""),
        starter_code=flat.get("starter_code", ""),
        optimal_solution=flat.get("optimal_solution", ""),
        brute_solution=flat.get("brute_solution", ""),
        examples=list(flat.get("examples") or []),
        constraints=list(flat.get("constraints") or []),
        tags=list(flat.get("tags") or []),
        function_signature=flat.get("function_signature", ""),
        test_cases=list(flat.get("test_cases") or []),
        source_slug=flat.get("source_slug", ""),
    )


class StoreGateway:
    def save(self, draft: ProblemDraft) -> int:
        """落库并返回 problem_id。"""
        from code_tutor_agent.db.database import save_problem

        return save_problem(draft_to_problem_dict(draft))

    def static_problem(self, topic: str, difficulty: str) -> ProblemDraft | None:
        """静态题库兜底：带参 → 无参。"""
        from code_tutor_agent.store.static_pool import get_static_problem

        flat = get_static_problem(topic=topic, difficulty=difficulty)
        if flat is None:
            flat = get_static_problem()
        return flat_to_draft(flat, topic, difficulty)

    def unac_problem(
        self, topic: str, difficulty: str, profile_hint: str | None = None,
    ) -> ProblemDraft | None:
        """历史未 AC 题（HISTORY 通道）：按主题/难度优先选未 AC 题。"""
        from code_tutor_agent.db.database import get_unac_problem

        pid = get_unac_problem(topic=topic, difficulty=difficulty, profile_hint=profile_hint)
        if pid is None:
            return None
        from code_tutor_agent.db.database import get_problem_by_id

        full = get_problem_by_id(pid)
        if full is None:
            return None
        return flat_to_draft(
            {
                "title": full.title,
                "topic": full.topic,
                "difficulty": full.difficulty,
                "description": full.description,
                "starter_code": full.starter_code,
                "optimal_solution": full.optimal_solution,
                "brute_solution": full.brute_solution,
                "function_signature": full.function_signature,
                "test_cases": full.test_cases,
                "constraints": full.constraints,
                "examples": list(full.examples or []),
                "tags": list(full.tags or []),
            },
            topic,
            difficulty,
        )

    def get_problem(self, problem_id: int):
        """按 id 读回题目（后台用例生成用）；不存在返回 None。"""
        from code_tutor_agent.db.database import get_problem_by_id

        return get_problem_by_id(problem_id)

    def update_test_cases(
        self, problem_id: int, test_cases: list[dict], visible: list[dict],
    ) -> None:
        """回写全量 / 可见用例（后台用例生成收尾）。"""
        from code_tutor_agent.db.database import update_problem_test_cases

        update_problem_test_cases(problem_id, test_cases, visible)
