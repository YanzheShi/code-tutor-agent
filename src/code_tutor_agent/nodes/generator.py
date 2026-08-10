"""生成器节点 — 出题子 Agent 的唯一适配器/翻译器（薄壳）。

**流程图（代码注释中确保可追溯）：**

    START → planner_node → generator_node → wait_for_submit_node
                                                     │
                                            [用户写代码]
                                                     │
                                                     ▼
                                              judge_node（使用全量测试用例）
                                                     │
                                                     ▼
                                              tutor_node → planner / wait

**设计（docs/generation-subagent-design.md §2/§12）：**

generator_node 是"翻译器"，不参与任何出题决策：
- 调 ProblemGenerationAgent().run(GenerationContext(...)) 完成决策树出题
  （LLM 原创 → 重试 → LeetCode 按主题拉题 → 历史未 AC 题 → 静态题库）
- 把 GenerationResult 翻译成 Command(update=..., goto="wait_for_submit_node")

所有决策逻辑在 generation/ 包（可独立单测，零 LangGraph 耦合）。
题库完整用例由后台（API 层或 generation 包后台任务）补全，不挡首屏。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from langgraph.config import get_stream_writer
from langgraph.types import Command

from code_tutor_agent.generation import ProblemGenerationAgent
from code_tutor_agent.generation.gateways.llm import (
    normalize_topic_for_generation,  # noqa: F401  # 兼容再导出
)
from code_tutor_agent.generation.state import GenerationContext, GenerationResult, GenEvent
from code_tutor_agent.progress import _generation_progress, record_generation_channel
from code_tutor_agent.sandbox.ds import get_struct_prologue
from code_tutor_agent.schemas.state import (
    Message as TutorMsg,
)
from code_tutor_agent.schemas.state import (
    ProblemMeta,
    SessionPhase,
    SessionState,
)

logger = logging.getLogger(__name__)

# MODEL_ALIAS constant removed — model selection now via config.PURPOSE_CONFIGS

# ── Topic → Tag enum 映射 ──
_TOPIC_TAG_MAP: dict[str, str] = {
    "数组": "array_basics",
    "数组+哈希表": "array_basics",
    "双指针": "array_two_pointers",
    "滑动窗口": "array_sliding_window",
    "二分查找": "array_binary_search",
    "链表": "linkedlist_basics",
    "栈": "stack_basics",
    "队列": "queue_deque",
    "动态规划": "dp_1d",
    "字符串": "string_basics",
    "递归": "backtrack",
    "贪心": "greedy",
    "位运算": "bit_manip",
    "排序": "array_sorting",
    "前缀和": "array_prefix_sum",
    # ── 图 / 树 / 堆 / 并查集 等（取值须为合法 Tag 枚举，避免默认 array_basics）──
    "图": "graph_dfs",
    "图论": "graph_dfs",
    "图遍历": "graph_dfs",
    "图的dfs": "graph_dfs",
    "图的bfs": "graph_bfs",
    "图bfs": "graph_bfs",
    "拓扑排序": "graph_topo",
    "最短路径": "graph_dfs",
    "并查集": "union_find",
    "树": "tree_dfs",
    "树结构": "tree_dfs",
    "二叉树": "tree_bfs",
    "线段树": "tree_bfs",
    "跳表": "jump_list",
    "堆": "heap_priority_queue",
    "优先队列": "heap_priority_queue",
    "回溯": "backtrack",
    "数论": "math_number_theory",
}


def tag_for(topic: str) -> str:
    """Map a Chinese topic name to a Tag enum value."""
    return _TOPIC_TAG_MAP.get(topic, "array_basics")


def elo_for(difficulty: str) -> int:
    """Map difficulty string to ELO rating."""
    return {"easy": 1200, "medium": 1500, "hard": 1800}.get(difficulty, 1200)


def _progress(sid: str, msg: str):
    """Write a progress message for a session (thread-safe shared dict)."""
    _generation_progress.setdefault(sid, []).append(msg)


# ── Tag enum value → 中文 topic（弱项提示用，供 HISTORY 通道优先）──
_TAG_TO_TOPIC_HINT: dict[str, str] = {
    "array_basics": "数组",
    "array_two_pointers": "双指针",
    "array_sliding_window": "滑动窗口",
    "array_binary_search": "二分查找",
    "array_prefix_sum": "前缀和",
    "array_sorting": "排序",
    "linkedlist_basics": "链表",
    "stack_basics": "栈",
    "queue_deque": "队列",
    "dp_1d": "动态规划",
    "string_basics": "字符串",
    "backtrack": "递归",
    "greedy": "贪心",
    "bit_manip": "位运算",
    "tree_dfs": "树",
    "graph_dfs": "图",
    "union_find": "并查集",
    "heap_priority_queue": "堆",
}


def _profile_hint_from(state: SessionState) -> str | None:
    """从 v2 画像提取最弱 tag 的中文 topic（HISTORY 优先级用）；无画像返回 None。"""
    try:
        from code_tutor_agent.db.database import get_user_profile_v2

        profile = get_user_profile_v2()
        prof = profile.get("prof") or {}
        if not prof:
            return None
        weakest = min(prof, key=lambda t: float(prof[t]))
        return _TAG_TO_TOPIC_HINT.get(weakest)
    except Exception as exc:  # noqa: BLE001
        logger.warning("profile hint 读取失败: %s", exc)
        return None


_GEN_AGENT = ProblemGenerationAgent()

# 事件 kind → 前端 progress 前缀
_KIND_PREFIX = {"progress": "", "warning": "⚠️ ", "error": "❌ ", "info": "📝 "}


class _SessionSink:
    """把 generation 包的 GenEvent 翻译成会话进度（_generation_progress + stream writer）。"""

    def __init__(self, sid: str, writer):
        self.sid = sid
        self.writer = writer

    def event(self, ev: GenEvent) -> None:
        text = f"{_KIND_PREFIX.get(ev.kind, '')}{ev.message}"
        _progress(self.sid, text)
        self.writer(text)


def generator_node(state: SessionState) -> Command[Literal["wait_for_submit_node"]]:
    """出题子 Agent 适配器：决策逻辑全部在 generation/ 包，此处仅翻译。

    Graph flow (see module docstring):
        planner_node → generator_node [HERE] → wait_for_submit_node
    """
    logger.info("▶ generator_node() — topic=%s, difficulty=%s", state.topic, state.difficulty)
    sid = state.session_id
    writer = get_stream_writer() or (lambda *a, **k: None)
    sink = _SessionSink(sid, writer)

    lc_url = (state.leetcode or {}).get("url") or ""
    ctx = GenerationContext(
        topic=state.topic,
        difficulty=state.difficulty,
        lc_url=lc_url or None,
        leetcode=state.leetcode,
        profile_hint=_profile_hint_from(state),
    )
    result = _GEN_AGENT.run(ctx, sink)
    record_generation_channel(sid, result.channel)

    if result.ok:
        return _translate_to_command(state, result)
    return _translate_error(state, sid, writer, result)


# ── 翻译：GenerationResult → Command ──

_LC_CHANNELS = {"leetcode_import", "leetcode_pull"}


def _translate_to_command(
    state: SessionState, result: GenerationResult
) -> Command[Literal["wait_for_submit_node"]]:
    draft = result.draft
    topic = state.topic
    difficulty = draft.difficulty or state.difficulty

    # ── 注入树/图/链表结构体定义到 starter_code（镜像现状行为）──
    starter = draft.starter_code
    struct_prologue = get_struct_prologue(topic, draft.description, starter)
    if struct_prologue and not starter.startswith(struct_prologue.strip()[:20]):
        starter = struct_prologue + starter

    visible_tcs = [dict(tc) for tc in draft.test_cases if not tc.get("is_hidden", False)][:4]

    meta = ProblemMeta(
        problem_id=result.problem_id,
        title=draft.title,
        topic=topic,
        difficulty=difficulty,
        description=draft.description,
        description_html=(
            (state.leetcode or {}).get("description_html", draft.description)
            if state.leetcode else draft.description
        ),
        starter_code=starter,
        visible_test_cases=visible_tcs,
        novelty_score=9.0 if draft.from_leetcode else 7.0,
        tag_primary=tag_for(topic),
        prob_elo=elo_for(difficulty),
    )

    if result.channel in _LC_CHANNELS:
        welcome_msg = TutorMsg(
            role="tutor",
            content=f"来自 LeetCode 的 **{meta.title}** 🎯  \n\n"
                    f"难度: {meta.difficulty} | 标签: {', '.join(draft.tags) or '—'}\n\n"
                    f"编辑器里已填入模板代码。写完点「运行」看示例结果，点「提交」正式判题。",
        )
    else:
        welcome_msg = TutorMsg(
            role="tutor",
            content=(
                f"来，试试这道 **{meta.title}**！\n\n"
                "编辑器里已填入模板代码。写完点「运行」看示例结果，点「提交」正式判题。"
            ),
        )

    # Agent 模式：保留出题前对话（连续对话），普通模式仅 welcome
    _tutor_msgs = (list(state.tutor_messages) if state.mode == "agent" else []) + [welcome_msg]
    update: dict[str, Any] = {
        "problem": meta,
        "status": "awaiting_submit",
        "phase": SessionPhase.solving,
        "submissions": [],
        "hint_level": 0,
        "tutor_messages": _tutor_msgs,
        "last_verdict": None,
        "adversarial_triggered": False,
        "error_message": "",
        "leetcode": None,  # Clear so it's not reprocessed
        # Store for background test generation (API layer reads these)
        "_brute_code": draft.optimal_solution or draft.brute_solution,
        "_function_signature": draft.function_signature,
        "_problem_id": result.problem_id,
    }

    return Command(update=update, goto="wait_for_submit_node")


def _translate_error(
    state: SessionState, sid: str, writer, result: GenerationResult
) -> Command[Literal["__end__"]]:
    """全线失败（LLM→PULL→HISTORY→STATIC）→ status=error + 友好提示。"""
    logger.error("generator_node 出题失败 — session=%s topic=%s chain=%s",
                 sid, state.topic, result.fallback_chain)
    _err_msg = (
        f"出题失败:{result.error or '所有可用通道均失败'}。"
        "请重新生成一次，或者换个主题/难度再试。"
    )
    _progress(sid, f"❌ {_err_msg}")
    writer(f"❌ {_err_msg}")
    return Command(
        update={
            "status": "error",
            "error_message": _err_msg,
            "phase": SessionPhase.solving,
        },
        goto="__end__",
    )
