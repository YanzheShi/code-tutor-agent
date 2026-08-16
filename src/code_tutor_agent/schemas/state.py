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


def last_phase(current: "SessionPhase | None", update: "SessionPhase | list") -> "SessionPhase":
    """phase 通道的 reducer：同一步内多个 node 写 phase 时取最后一个（最新当前阶段）。

    默认 last_value 通道在「同一步收到多个写」时会抛
    InvalidUpdateError("Can receive only one value per step")；用 reducer 允许这种情况，
    语义上 phase 就是「当前阶段」，last-wins 正确。
    """
    if isinstance(update, list):
        return update[-1]
    return update


def last_wins_list(current: list, update: list) -> list:
    """list 通道的 last-wins reducer：同一步多个写入者时取最后一个值。

    与 last_phase 同理：默认 last_value 通道在「同一步收到多个写」时会抛
    InvalidUpdateError("Can receive only one value per step")。
    tutor_messages / agent_dialog_history / problem_history 都是多写者字段
    （graph 节点出口 + HTTP 端点的 pause_safe_update 都会写），必须用 reducer。

    ⚠️ 必须用 last-wins 而不是 operator.add：所有写入者传的都是「全量列表」
    （state.xxx + 本次新增，见 tutor.py / generator.py / agent_judge.py / chat.py），
    operator.add 会让历史被重复追加（2026-08-07 实测 4 轮翻到 30 条）；last-wins
    与现有覆盖语义完全一致，只是不再抛错。
    """
    return update


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
    # ── 题面展示字段（LLM 生成的题，约束/示例在独立字段，不混进 description）──
    constraints: list[str] = Field(default_factory=list, description="输入参数限制，如 '1 <= nums[i] <= 10^4'")
    examples: list[str] = Field(default_factory=list, description="原始示例文本（LLM 输出）")

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
    # ── Bug 2: 结构化失败用例（首个失败 test case），供前端「期望 vs 实际」对比面板 ──
    input_args: list[str] = Field(default_factory=list, description="Input args of the first failing test case")
    expected_output: str = Field(default="", description="Expected output of the first failing test case")
    actual_output: str = Field(default="", description="Actual output of the first failing test case")
    explanation: str = Field(default="", description="该用例的语义说明（测试用例自带 explanation）")


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
    is_run: bool = Field(
        default=False,
        description="True if this submission came from the 运行 button (sample scope), "
                    "not a graded 提交. Run results are diagnostic only and never written to profile.",
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
    dialog = "dialog"           # Agent 模式：导师对话确定需求（出题前）
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
        default="agent",
        description="Session mode: agent-driven (dialog + MCP judging). "
                    "normal modes (practice/interview/debug_theatre) removed — all sessions are agent.",
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
    tutor_messages: Annotated[list[Message], last_wins_list] = Field(
        default_factory=list,
        description=(
            "Conversation visible in the tutor panel. "
            "普通模式：单题维度，换题时由 critic_node 清空。 "
            "Agent 模式：整段使用周期的连续对话（出题前→做题中→反馈→下一题/放弃），"
            "全程不清空，generator_node 仅在其后追加 welcome 消息。"
        ),
    )

    # ── Routing hints (internal, set by Judge → consumed by Tutor) ──
    last_verdict: Optional[str] = Field(
        default=None,
        description="Shortcut: latest judge verdict for easy routing",
    )

    # ── Agent mode fields ──
    # These are only used when mode == "agent"

    agent_dialog_history: Annotated[list[Message], last_wins_list] = Field(
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
    judge_scope: Literal["sample", "full"] = Field(
        default="full",
        description="Scope of the latest judge: 'sample' (运行, visible cases only, "
                    "diagnostic, no profile) or 'full' (提交, all cases incl. boundary).",
    )

    # ── Error handling ──
    error_message: str = Field(default="", description="Populated when status=error")

    # ── 生成进度不放 state：走 api/progress.py 的 `_generation_progress`
    # （线程安全共享 dict）+ /progress/stream SSE 推送。原 `progress_messages`
    # 状态字段从无节点写入，2026-08-04 已移除。──

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

    # ── Run results (set by 运行 endpoint via agent_judge_node, scope=sample) ──
    last_run_results: list = Field(
        default_factory=list,
        description="Diagnostic results of the latest 运行 (sample scope). Replaced each run; never written to profile.",
    )

    # ── Profile module delta ──
    profile_delta: Optional[dict] = Field(
        default=None,
        description="Profile delta produced by judge_node, consumed by update_profile_node",
    )

    # ── Phase（前端消费态，node 出口写）──
    # 多个 node（generator / planner / agent_tutor / critic）都会写 phase。
    # 在部分多轮状态下，两个写者会落进 langgraph 的「同一图步」，默认的 last_value
    # 通道会抛 InvalidUpdateError("Can receive only one value per step")。
    # 因此用 reducer：同一步内多写时取最后一个（即最新的当前阶段），语义正确。
    phase: Annotated[SessionPhase, last_phase] = Field(
        default=SessionPhase.solving,
        description="Frontend-facing phase: solving / reviewing / done",
    )

    # ── 跨题历史（每 flush 一题 +1）──
    problem_history: Annotated[list[ProblemAttemptRecord], last_wins_list] = Field(
        default_factory=list,
        description="All completed problem records, in order",
    )
    total_problems: int = Field(
        default=0, ge=0,
        description="Total problems completed in this session",
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

    # ── 上下文摘要（滑动窗口 + 摘要策略）──
    context_summary: Optional[str] = Field(
        default=None,
        description=(
            "压缩后的历史对话摘要。当对话 token 超预算时，"
            "旧消息被 LLM 压缩到此字段，新消息保留原文。"
            "Agent 模式换题时生成跨题摘要存入此字段。"
        ),
    )