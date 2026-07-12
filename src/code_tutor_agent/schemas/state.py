"""LangGraph SessionState 及相关类型。

这是流经 StateGraph 的会话状态的**单一数据源**。
每个节点读写这些字段。

**各模式生命周期：**

普通模式（practice / interview）：
    START → planner_node → generator_node → wait_for_submit_node
          → judge_node → tutor_node →（循环回到 wait）

Agent 模式：
    START → agent_dialog_node（多轮对话收集需求）
          → planner_node → generator_node → wait_for_submit_node
          → agent_judge_node（LLM + MCP 判题）
          → agent_tutor_node（温暖反馈 + 修复建议）
          →（AC → done）|（未通过 → wait_for_submit_node 循环）
"""

from __future__ import annotations

import operator
from enum import Enum
from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
#  Sub-types carried inside SessionState
# ──────────────────────────────────────────────


class ProblemMeta(BaseModel):
    """Lightweight problem descriptor carried in session state."""

    problem_id: int = Field(description="Primary key in the problems table")
    title: str = Field(description="Problem title")
    topic: str = Field(description="Knowledge-point tag, e.g. '双指针'")
    difficulty: str = Field(description="easy / medium / hard")
    description: str = Field(description="Full problem statement text")
    starter_code: str = Field(default="", description="LeetCode-style template stub for the editor")
    visible_test_cases: list[dict] = Field(default_factory=list, description="Non-hidden test cases")
    description_html: str = Field(default="", description="Original HTML version of description")

    # ── Profile module fields ──
    tag_primary: str = Field(default="array_basics", description="Profile Tag enum value")
    prob_elo: int = Field(default=1200, description="Problem ELO difficulty for profile scoring")

    # ── 暗数据 ──
    novelty_score: float = Field(default=7.0, ge=0.0, le=10.0, description="Novelty rating")


class JudgeResult(BaseModel):
    """Outcome of one judge pass (base or adversarial or review).

    D3: 支持多阶段判题。每个 Submission 可以包含多个 JudgeResult，
    分别对应 base / adversarial_scale / adversarial_boundary / review 阶段。

    面试考点：为什么每个阶段一个 JudgeResult 而不是一个大的？
        — 路由需要细粒度控制：基础挂了→辅导，对抗挂了→辅导但提示不同，
          全部通过→评审。每个阶段的 verdict 独立决定下一跳。
    """

    status: Literal["AC", "WA", "TLE", "RE", "CE"] = Field(
        description="Verdict: Accepted / Wrong Answer / TLE / Runtime Error / Compile Error"
    )
    phase: Literal["base", "adversarial_scale", "adversarial_boundary", "review"] = Field(
        default="base",
        description="Which phase produced this result",
    )
    detail: str = Field(default="", description="Human-readable reason / diff info")
    runtime_ms: float = Field(default=0.0, description="Execution time in milliseconds")
    memory_kb: float = Field(default=0.0, description="Peak memory in kilobytes")


class Submission(BaseModel):
    """One code submission from the user, stored in order."""

    index: int = Field(description="1-based submission number within this session")
    code: str = Field(description="Source code submitted by the user")
    language: str = Field(default="python", description="Programming language")
    verdict: str = Field(
        default="",
        description="Collapsed verdict for this submission (AC/WA/TLE/RE/CE), set by judge node",
    )
    timestamp: str = Field(
        default="",
        description="ISO-8601 timestamp when the submission was created",
    )
    judge_results: list[JudgeResult] = Field(
        default_factory=list,
        description="Results from base + adversarial judge passes",
    )
    hint_level_given: int = Field(
        default=0, ge=0, le=4,
        description="Hint level the tutor gave *after* this submission",
    )


class Message(BaseModel):
    """A single message in the tutor conversation pane."""

    role: Literal["user", "tutor", "system"] = Field(description="Who said it")
    content: str = Field(description="Message body")
    metadata: dict = Field(default_factory=dict, description="Extra structured data")


# ──────────────────────────────────────────────
#  Multi-turn (连续做题) sub-types
# ──────────────────────────────────────────────


class SessionPhase(str, Enum):
    """前端消费态，node 出口写，checkpointer 托管。"""
    clarifying = "clarifying"   # V0.1 不写，V0.2 才用
    solving = "solving"         # 用户正在写代码
    reviewing = "reviewing"     # 辅导态（WA 线 tutor / AC 线 agent_tutor）
    done = "done"               # 换题前短暂过渡


class DiagnosisSummary(BaseModel):
    """判题→辅导链给的误解标签聚合（单题纬度）。"""
    primary_error: str = Field(default="", description="主要误解类型")
    tags: list[str] = Field(default_factory=list, description="误解标签列表")
    hint_level_reached: int = Field(default=0, ge=0, le=4)
    rounds_in_tutor: int = Field(default=0, description="本题辅导轮数")
    resolved: bool = Field(default=False, description="用户是否表示懂了")


class ProblemAttemptRecord(BaseModel):
    """单题生命周期快照。
    不存 tutor_messages 全文，回放走 checkpointer。
    """
    problem_id: int = 0
    title: str = ""
    tags: list[str] = Field(default_factory=list)       # tag_primary 值（Tag enum）
    difficulty: str = ""
    verdict: str = ""                                     # AC / WA / ABANDON
    user_code_final: str = Field(default="", description="最后一版提交代码")
    hint_level_reached: int = 0
    tutor_messages_count: int = 0
    diagnosis: Optional[DiagnosisSummary] = None
    judge_report: Optional[dict] = None
    started_at: str = ""
    ended_at: str = ""
    abandoned: bool = False


# ──────────────────────────────────────────────
#  Main session state
# ──────────────────────────────────────────────


class SessionState(BaseModel):
    """LangGraph conversational state for one tutoring session.

    Every node in the graph reads/writes these fields through the
    checkpointer-managed state dictionary.

    **Lifecycle**:

    Normal mode:
        1. Planner sets *problem* → status = ``awaiting_submit``
        2. User submits code → Judge runs, Tutor gives hint
        3. Loop until AC → status = ``done``

    Agent mode:
        1. agent_dialog_node 多轮对话 → status = ``dialog``
        2. 确定 topic/difficulty → planner → generator
        3. 用户提交 → agent_judge_node → agent_tutor_node
        4. 未通过 → 回到 wait_for_submit (循环)
        5. AC → done
    """

    session_id: str = Field(description="Unique session / thread_id")
    # ── User preferences (set on create, consumed by planner+generator) ──
    topic: str = Field(default="数组", description="User-selected knowledge point")
    difficulty: str = Field(default="easy", description="User-selected difficulty")
    mode: Literal["practice", "interview", "debug_theatre", "agent"] = Field(
        default="practice",
        description="Session mode: normal modes vs agent-driven (dialog + MCP judging)",
    )
    status: Literal[
        "awaiting_problem", "awaiting_submit", "judging",
        "tutoring", "dialog", "done", "error",
    ] = Field(default="awaiting_problem")

    # ── Problem ──
    problem: Optional[ProblemMeta] = Field(
        default=None,
        description="Current problem (set by Generator node)",
    )

    # ── Submissions ──
    submissions: Annotated[list[Submission], operator.add] = Field(
        default_factory=list,
        description="All submissions, in chronological order",
    )

    # ── Tutor state ──
    hint_level: int = Field(
        default=0, ge=0, le=4,
        description="Current hint level (0=no hint yet, 4=almost answer)",
    )
    tutor_messages: list[Message] = Field(
        default_factory=list,
        description="Conversation visible in the tutor panel (单题维度，换题时清)",
    )

    # ── Routing hints (internal, set by Judge → consumed by Tutor) ──
    last_verdict: Optional[str] = Field(
        default=None,
        description="Shortcut: latest judge verdict for easy routing",
    )
    adversarial_triggered: bool = Field(
        default=False,
        description="Did the adversarial phase run on the latest AC submission?",
    )

    # ── Agent mode fields ──
    # These are only used when mode == "agent"

    agent_dialog_history: Annotated[list[Message], operator.add] = Field(
        default_factory=list,
        description="Agent mode: dialog transcript before problem generation (出题前对话)",
    )
    agent_dialog_complete: bool = Field(
        default=False,
        description="Agent mode: set to True when dialog is done and we're ready to generate a problem",
    )
    repair_suggestion: str = Field(
        default="",
        description="Agent mode: repair suggestion from the last agent judge cycle",
    )
    warm_feedback: str = Field(
        default="",
        description="Agent mode: warm feedback from the last agent judge cycle",
    )
    judge_cycle: int = Field(
        default=0,
        description="Agent mode: how many judge cycles have completed (1-based)",
    )

    # ── Error handling ──
    error_message: str = Field(default="", description="Populated when status=error")

    # ── 生成进度（前端轮询显示）──
    progress_messages: Annotated[list[str], operator.add] = Field(
        default_factory=list,
        description="Progress log during generation, e.g. ['正在生成题目…', '自验证通过…']",
    )

    # ── LeetCode import (set when user provides a LC URL) ──
    leetcode: Optional[dict] = Field(
        default=None,
        description="Parsed LeetCode problem data from /leetcode/parse endpoint",
    )

    # ── Review / extra (set by judge, consumed by tutor) ──
    last_review_payload: Optional[dict] = Field(
        default=None,
        description="Most recent code review payload (set by judge, consumed by tutor)",
    )

    # ── Run results (set by POST /session/{sid}/run) ──
    last_run_results: Annotated[list, operator.add] = Field(
        default_factory=list,
        description="Most recent run-code results (survives page reload); set by run endpoint",
    )

    # ── Profile module delta ──
    profile_delta: Optional[dict] = Field(
        default=None,
        description="Profile delta produced by judge_node, consumed by update_profile_node",
    )

    # ── LangChain message history ──
    messages: list = Field(
        default_factory=list,
        description="LangChain-style message list (HumanMessage, AIMessage). "
                    "Managed by InMemorySaver checkpointer, not manually.",
    )

    # ── Phase（前端消费态，node 出口写）──
    phase: SessionPhase = Field(
        default=SessionPhase.solving,
        description="Frontend-facing phase: solving / reviewing / done",
    )

    # ── 跨题历史（每 flush 一题 +1）──
    problem_history: list[ProblemAttemptRecord] = Field(
        default_factory=list,
        description="All completed problem records, in order",
    )
    total_problems: int = Field(
        default=0, ge=0,
        description="Total problems completed in this session",
    )

    # ── Tutor micro-loop（D4 才用，先加字段）──
    turns_in_level: int = Field(
        default=0, ge=0,
        description="How many tutor turns at current hint_level",
    )
    last_router_decision: Optional[dict] = Field(
        default=None,
        description="Last TutorRouterDecision from tutor_router node",
    )
    tutor_mode: Literal["normal", "agent"] = Field(
        default="normal",
        description="normal = L0-L4 micro-loop; agent = AC复盘单发",
    )

    # ── /next-problem 临时信号（消费即清）──
    pending_abandon: bool = Field(
        default=False,
        description="True when /next-problem needs to flush ABANDON",
    )
    next_preference: Optional[Literal["same_topic", "next_in_plan", "random"]] = Field(
        default=None,
        description="Planner topic selection preference",
    )

    # ── 上一题 diagnosis（critic flush 后清，供下一题 tutor 承接）──
    last_diagnosis: Optional[DiagnosisSummary] = Field(
        default=None,
        description="Most recent problem diagnosis summary",
    )