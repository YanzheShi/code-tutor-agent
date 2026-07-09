"""FastAPI 请求 / 响应 Schema。

这些**不属于** LangGraph state —— 它们是 HTTP 层与客户端之间的通信契约。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
#  Request bodies
# ──────────────────────────────────────────────


class SubmitRequest(BaseModel):
    """User submits code for judging."""

    code: str = Field(description="User's source code")
    language: str = Field(default="python", description="Programming language")


class CreateSessionRequest(BaseModel):
    """Optional hints when creating a new session."""

    user_id: Optional[str] = Field(
        default=None,
        description="Cross-session user identifier (for profile lookup)",
    )
    topic: Optional[str] = Field(
        default=None,
        description="Knowledge point, e.g. 数组, 双指针, 动态规划",
    )
    difficulty: Optional[str] = Field(
        default=None,
        description="easy / medium / hard",
    )
    mode: Optional[str] = Field(
        default="practice",
        description="practice / interview / debug_theatre",
    )
    leetcode: Optional[dict] = Field(
        default=None,
        description="Parsed LeetCode problem data (from /leetcode/parse)",
    )


# ──────────────────────────────────────────────
#  Response bodies
# ──────────────────────────────────────────────


class SessionStateResponse(BaseModel):
    """What the frontend fetches via GET /session/{sid}/state."""

    session_id: str
    topic: str = ""
    difficulty: str = ""
    mode: str = "practice"
    status: str
    problem: Optional[dict] = None
    submissions: list[dict] = Field(default_factory=list)
    tutor_messages: list[dict] = Field(default_factory=list)
    hint_level: int = 0
    last_verdict: Optional[str] = None
    last_review_payload: Optional[dict] = None
    progress_messages: list[str] = Field(default_factory=list)


class SubmitResponse(BaseModel):
    """Response after a submit -> judge -> tutor round."""

    session_id: str
    status: str
    verdict: Optional[str] = None
    tutor_message: Optional[str] = None
    hint_level: int = 0


# ──────────────────────────────────────────────
#  Run (visible test cases only)
# ──────────────────────────────────────────────


class RunCodeRequest(BaseModel):
    """User submits code for a quick test run (no judge/tutor)."""

    code: str = Field(description="User's source code")
    language: str = Field(default="python", description="Programming language")


class RunResult(BaseModel):
    """Result of a single test case."""

    test_case_id: int
    passed: bool
    status: str
    detail: str = ""
    input_args: list[str] = Field(default_factory=list)
    expected: str = ""
    runtime_ms: float = 0.0
    memory_kb: float = 0.0


class RunCodeResponse(BaseModel):
    """Response from a quick test run."""

    session_id: str
    all_passed: bool
    results: list[RunResult]
    total: int
    passed: int


# ──────────────────────────────────────────────
#  LeetCode parser
# ──────────────────────────────────────────────


class LeetCodeParseRequest(BaseModel):
    """Request to parse a LeetCode problem URL."""

    url: str = Field(description="LeetCode problem URL, e.g. https://leetcode.com/problems/two-sum/")


class LeetCodeParseResponse(BaseModel):
    """Result of parsing a LeetCode problem."""

    title: str
    url: str = ""
    description: str
    description_html: str = ""  # 原始 HTML 格式，前端渲染富文本
    difficulty: str
    examples: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    starter_code: str = ""
    hints: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    parsed_test_cases: list[dict] = Field(default_factory=list)
    session_id: str = ""


# ──────────────────────────────────────────────
#  Admin API schemas
# ──────────────────────────────────────────────


class AdminLoginRequest(BaseModel):
    """Admin password verification."""

    password: str = Field(description="Admin password from ADMIN_PASSWORD env var")


class AdminPasswordRequest(BaseModel):
    """Generic admin password verification."""

    password: Optional[str] = Field(default="", description="Admin password")


class AdminProblemOut(BaseModel):
    """Full problem representation for admin listing.

    Distinguishes two testcase categories:
    - visible_test_cases: shown to users in the "Run" tab (前台运行)
    - test_cases: full suite including hidden cases used by the judge (判题使用)
    """

    id: int
    title: str
    topic: str
    difficulty: str
    description: str
    visible_test_cases: list[dict] = Field(default_factory=list, alias="visible_test_cases_list")
    test_cases: list[dict] = Field(default_factory=list, alias="test_cases_list")
    brute_solution: str = ""
    starter_code: str = ""
    novelty_score: float = 7.0
    created_at: str = ""


class AdminUpdateProblemRequest(BaseModel):
    """Fields that can be updated via admin panel.

    Testcase fields:
    - test_cases: full judge suite (判题使用，含隐藏用例)
    - visible_test_cases: frontend run suite (前台运行，仅可见用例)
    When only test_cases is provided, visible_test_cases defaults
    to the non-hidden subset.
    """

    password: Optional[str] = Field(default="", description="Admin password for verification")
    title: Optional[str] = None
    description: Optional[str] = None
    topic: Optional[str] = None
    difficulty: Optional[str] = None
    test_cases: Optional[list[dict]] = None
    visible_test_cases: Optional[list[dict]] = None
    brute_solution: Optional[str] = None
    starter_code: Optional[str] = None
    novelty_score: Optional[float] = None