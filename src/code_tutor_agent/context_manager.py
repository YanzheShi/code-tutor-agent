"""上下文管理模块 — token 计数、滑动窗口、摘要压缩。

核心职责：
    1. token 计数（tiktoken）
    2. 滑动窗口裁剪（保留最近 N 轮原文）
    3. 旧消息摘要压缩（LLM 驱动）
    4. 跨题上下文构建（Agent 模式专用）

使用方式：
    from code_tutor_agent.context_manager import (
        ContextConfig,
        count_tokens,
        trim_history_window,
        build_transcript_with_budget,
        generate_summary,
        build_cross_problem_context,
    )
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from code_tutor_agent.schemas.state import Message, ProblemAttemptRecord

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
#  tiktoken 编码器（全局单例，避免重复加载）
# ═══════════════════════════════════════════════════════════════════
#
# cl100k_base 是 GPT-4 / GPT-3.5-turbo 的编码，与 agnes 模型近似。
# 精度误差 < 15%，可被 ContextConfig 中 25% 安全余量吸收。
#
try:
    import tiktoken

    _ENCODER = tiktoken.get_encoding("cl100k_base")
except Exception:
    logger.warning("tiktoken not available — token counting will use char-based fallback")
    _ENCODER = None


# ═══════════════════════════════════════════════════════════════════
#  Tag → 中文名称映射（与 agent_dialog / planner 保持一致）
# ═══════════════════════════════════════════════════════════════════

_TAG_CN: dict[str, str] = {
    "array_basics": "数组基础",
    "array_two_pointers": "双指针",
    "array_sliding_window": "滑动窗口",
    "array_binary_search": "二分查找",
    "array_prefix_sum": "前缀和",
    "array_sorting": "排序",
    "linkedlist_basics": "链表基础",
    "linkedlist_cycle": "环形链表",
    "stack_basics": "栈",
    "queue_deque": "队列",
    "dp_1d": "一维动态规划",
    "dp_multidim": "多维动态规划",
    "string_basics": "字符串",
    "backtrack": "回溯",
    "greedy": "贪心",
    "bit_manip": "位运算",
}


# ═══════════════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════════════


@dataclass
class ContextConfig:
    """Token 预算和滑动窗口配置。

    总预算 192K = 256K 上下文窗口 × 0.75，留 25% 安全余量。
    余量用于吸收：
        - tiktoken 与 agnes tokenizer 的编码差异
        - prompt 模板中的固定开销（题目描述、画像信息、系统指令）
        - 意外增长（用户粘贴的超长代码等）
    """

    # ── Token 预算 ──
    total_budget: int = 192_000
    # 滑动窗口：保留最近 N 轮完整对话原文（1 轮 = 用户消息 + 导师回复）
    window_turns: int = 20
    # system prompt + 画像信息等固定开销（按实际 prompt 模板长度估算）
    system_overhead: int = 2_000
    # 留给 LLM 输出的缓冲区（避免超出 max_output_tokens）
    response_reserve: int = 16_000
    # 单条摘要的最大 token 数（防止摘要自身过长）
    summary_max_tokens: int = 800
    # 用于生成摘要的模型别名
    summary_model: str = "agnes"
    # 触发摘要的阈值比例：当总 token 超过 available_budget × ratio 时压缩旧消息
    summary_trigger_ratio: float = 0.5
    # 强制触发滑动窗口的轮数上限（即使 token 未超预算，超过此轮数也裁剪）
    force_window_turns: int = 50

    @property
    def available_budget(self) -> int:
        """扣除系统开销和响应预留后的可用预算。

        这个值是真正可用于对话内容 + 摘要的 token 数。
        """
        return self.total_budget - self.system_overhead - self.response_reserve

    @property
    def summary_trigger_threshold(self) -> int:
        """当总 token 超过此阈值时触发摘要压缩。"""
        return int(self.available_budget * self.summary_trigger_ratio)


# 默认配置实例，各模块可直接引用
DEFAULT_CONFIG = ContextConfig()


# ═══════════════════════════════════════════════════════════════════
#  Token 计数
# ═══════════════════════════════════════════════════════════════════


def count_tokens(text: str) -> int:
    """使用 tiktoken 计算文本的 token 数。

    Args:
        text: 要计数的文本。

    Returns:
        token 数量。如果 tiktoken 不可用，降级为字符数量 ÷ 2.5 的估算
        （中英文混合场景下的经验常数）。
    """
    if _ENCODER is not None:
        return len(_ENCODER.encode(text))
    # 降级估算：中文约 1.5-2 字符/token，英文约 4 字符/token
    # 混合编程对话场景取 2.5
    return max(1, len(text) // 2)


def estimate_message_tokens(messages: list[Message]) -> int:
    """估算消息列表的总 token 数。

    每条消息除了 content 本身，还有拼接前缀（"用户:" / "AI导师:"）
    和换行符的开销（约 5 token）。这里统一按 +5 估算。

    Args:
        messages: 消息列表。

    Returns:
        预估总 token 数。
    """
    total = 0
    for msg in messages:
        total += count_tokens(msg.content) + 5
    return total


# ═══════════════════════════════════════════════════════════════════
#  滑动窗口裁剪
# ═══════════════════════════════════════════════════════════════════


def trim_history_window(
    messages: list[Message],
    context_summary: str | None,
    config: ContextConfig | None = None,
) -> tuple[list[Message], str | None]:
    """对消息列表应用滑动窗口策略。

    决策流程：
        ┌─ token 数 < 触发阈值 且 轮数 < 强制上限？
        │    → 不处理，直接返回原列表
        └─ token 超出或轮数超限？
             → 保留最近 N 轮原文，丢弃更早的消息
             → 如果已有摘要上下文，保持不更新
             → 如果还没有摘要，返回 None 让调用方决定是否生成

    注意：本函数只做裁剪决策，**不调用 LLM** 生成摘要。
    摘要生成需要调用方使用 :func:`generate_summary` 异步完成。

    Args:
        messages: 完整消息列表（未裁剪）。
        context_summary: 已有的上下文摘要，可能为 None。
        config: 预算配置，默认使用 DEFAULT_CONFIG。

    Returns:
        (trimmed_messages, summary) — 裁剪后的消息列表 + 更新后的摘要。
        摘要可能为 None（表示需要调用方生成新摘要）。
    """
    cfg = config or DEFAULT_CONFIG
    total = estimate_message_tokens(messages)
    msg_count = len(messages)

    # ── 情况 1：未超预算 → 原样返回 ──
    if total < cfg.summary_trigger_threshold and msg_count <= cfg.force_window_turns * 2:
        logger.debug(
            "Context within budget: %d tokens / %d threshold, %d messages",
            total, cfg.summary_trigger_threshold, msg_count,
        )
        return messages, context_summary

    # ── 情况 2：超预算 → 保留最近 N 轮 ──
    # 1 轮 = 2 条消息（用户 + 导师），所以 window_size = turns × 2
    window_size = cfg.window_turns * 2
    keep_start = max(0, msg_count - window_size)
    old_messages = messages[:keep_start]
    recent_messages = messages[keep_start:]

    logger.info(
        "Context overflow: %d tokens / %d threshold, %d messages → "
        "trimming %d oldest, keeping %d recent (%d turns window)",
        total, cfg.summary_trigger_threshold, msg_count,
        len(old_messages), len(recent_messages), cfg.window_turns,
    )

    # 如果丢弃了旧消息且没有已有摘要 → 需要调用方生成新摘要
    if old_messages and context_summary is None:
        logger.info("Old messages trimmed without summary — caller should generate one via generate_summary()")

    return recent_messages, context_summary


# ═══════════════════════════════════════════════════════════════════
#  Transcript 构建
# ═══════════════════════════════════════════════════════════════════


def _to_msg_dict(msg) -> dict:
    """将 Message 对象 / dict / 字符串统一转为 {'role', 'content'} 字典。

    防御性处理：兼容 Message 对象（Pydantic）、dict、str 三种类型。
    """
    if isinstance(msg, dict):
        return {"role": msg.get("role", "tutor"), "content": msg.get("content", "")}
    if hasattr(msg, "role") and hasattr(msg, "content"):
        return {"role": msg.role, "content": msg.content}
    return {"role": "tutor", "content": str(msg)}


def build_transcript_with_budget(
    history: list[Message],
    context_summary: str | None = None,
    config: ContextConfig | None = None,
) -> str:
    """构建带 token 预算管理的对话 transcript。

    这是 ``agent_dialog._build_transcript()`` 的增强替代版。

    流程：
        1. 对消息列表应用滑动窗口裁剪（保留最近 N 轮原文）
        2. 将裁剪后的消息格式化为 "用户: ...\\nAI导师: ..." 文本
        3. 如果有摘要上下文，拼接到 transcript 开头
        4. 最终安全检查：如果仍超预算，二次硬截断（保底安全网）

    Args:
        history: 完整对话历史。
        context_summary: 已有的上下文摘要（来自之前的裁剪操作）。
        config: 预算配置。

    Returns:
        格式化的 transcript 字符串，保证 token 数不超过 available_budget。
    """
    cfg = config or DEFAULT_CONFIG

    # Step 1: 滑动窗口裁剪
    trimmed, summary = trim_history_window(history, context_summary, cfg)

    # Step 2: 格式化为 transcript 行
    lines = []
    for msg in trimmed:
        d = _to_msg_dict(msg)
        prefix = "用户" if d["role"] == "user" else "AI导师"
        lines.append(f"{prefix}: {d['content']}")

    transcript = "\n".join(lines)

    # Step 3: 拼接摘要（如果有）
    if summary:
        transcript = (
            f"## 对话摘要（之前的对话要点）\n\n{summary}\n\n"
            f"---\n\n"
            f"## 近期对话\n\n{transcript}"
        )

    # Step 4: 最终安全检查 — 二次硬截断
    # 理论上不会发生（滑动窗口已保证），但保留作为安全网
    token_count = count_tokens(transcript)
    if token_count > cfg.available_budget:
        logger.warning(
            "Transcript still over budget after trimming: %d tokens > %d budget. "
            "Applying fallback hard truncation.",
            token_count, cfg.available_budget,
        )
        # 从最旧的消息开始丢弃，直到合预算
        while token_count > cfg.available_budget and len(trimmed) > 1:
            trimmed = trimmed[1:]
            lines = []
            for msg in trimmed:
                d = _to_msg_dict(msg)
                prefix = "用户" if d["role"] == "user" else "AI导师"
                lines.append(f"{prefix}: {d['content']}")
            transcript = "\n".join(lines)
            if summary:
                transcript = (
                    f"## 对话摘要（之前的对话要点）\n\n{summary}\n\n"
                    f"---\n\n"
                    f"## 近期对话\n\n{transcript}"
                )
            token_count = count_tokens(transcript)

    return transcript


# ═══════════════════════════════════════════════════════════════════
#  摘要生成（LLM 驱动）
# ═══════════════════════════════════════════════════════════════════

# 单题对话摘要的 system prompt
_SUMMARY_SYSTEM_PROMPT = """你是对话摘要专家。请将以下编程辅导对话压缩成简短的要点总结。

要求：
1. 记录用户学过的算法知识点（如"两数之和"、"哈希表"）
2. 标注判题结果（AC 通过 / WA 未通过 / 放弃）+ 具体的薄弱环节
3. 保留用户表达的学习偏好或困难（如"对递归不熟"、"边界条件总忘"）
4. 不需要保留逐轮对话细节，只提取核心结论和关键转折点
5. 中文输出，控制在 3-5 句话以内

输出纯文本，无需任何 JSON 或代码块格式。"""

# 跨题进度摘要的 system prompt
_CROSS_PROBLEM_SUMMARY_SYSTEM = """你是学习进度记录专家。请根据用户已完成的题目列表，生成一份学习进度摘要。

要求：
1. 按顺序列出每道题的标题、难度、知识点和结果（通过/未通过/放弃）
2. 标注用户反复出错的薄弱知识点（如果同一知识点多次 WA）
3. 如果发现了薄弱模式，指出需要重点加强的方向
4. 中文输出，简洁明了，控制在 200 字以内

输出纯文本，无需 JSON。"""


def _messages_to_transcript(messages: list[Message]) -> str:
    """将消息列表转为 transcript 文本（不经过预算管理）。

    与 _build_transcript 类似，但会对单条超长消息做截断
    （防止用户粘贴的大段代码撑满摘要 prompt）。
    """
    lines = []
    for msg in messages:
        d = _to_msg_dict(msg)
        prefix = "用户" if d["role"] == "user" else "AI导师"
        content = d["content"]
        # 截断超长单条消息（如代码粘贴），保留语义
        if len(content) > 2000:
            content = content[:2000] + "...[内容过长已截断]"
        lines.append(f"{prefix}: {content}")
    return "\n".join(lines)


def generate_summary(
    messages: list[Message],
    problem_record: ProblemAttemptRecord | None = None,
    model_alias: str | None = None,
    config: ContextConfig | None = None,
) -> str:
    """使用 LLM 生成对话摘要（同步调用）。

    Args:
        messages: 需要压缩的对话消息。
        problem_record: 关联的题目记录，用于在摘要中标注题目信息。
        model_alias: LLM 模型别名，默认使用 config.summary_model。
        config: 上下文配置。

    Returns:
        摘要文本。如果 LLM 调用失败，降级为基于规则的简单摘要。
    """
    cfg = config or DEFAULT_CONFIG
    model = model_alias or cfg.summary_model

    transcript = _messages_to_transcript(messages)

    # 拼接题目信息（如果有）
    user_text = f"## 需要总结的对话\n\n{transcript}"
    if problem_record:
        tags_cn = [_TAG_CN.get(t, t) for t in (problem_record.tags or [])]
        user_text = (
            f"## 题目信息\n"
            f"- 标题：{problem_record.title}\n"
            f"- 难度：{problem_record.difficulty}\n"
            f"- 知识点：{', '.join(tags_cn) if tags_cn else '未知'}\n"
            f"- 最终判题结果：{problem_record.verdict}\n"
            f"- 辅导提示等级：L{problem_record.hint_level_reached}\n"
            f"- 辅导轮数：{problem_record.tutor_messages_count}\n\n"
            f"{user_text}"
        )

    try:
        from code_tutor_agent.config import get_llm

        llm = get_llm(model, temperature=0.3, max_tokens=cfg.summary_max_tokens)
        resp = llm.invoke([
            ("system", _SUMMARY_SYSTEM_PROMPT),
            ("human", user_text),
        ])
        content = resp.content if hasattr(resp, "content") else str(resp)
        result = content.strip()
        if result:
            logger.info("Summary generated: %d chars from %d messages", len(result), len(messages))
            return result
    except Exception as exc:
        logger.warning("LLM summary failed: %s — falling back to rule-based summary", exc)

    return _rule_based_summary(messages, problem_record)


async def generate_summary_async(
    messages: list[Message],
    problem_record: ProblemAttemptRecord | None = None,
    model_alias: str | None = None,
    config: ContextConfig | None = None,
) -> str:
    """使用 LLM 异步生成对话摘要。

    Args 同 :func:`generate_summary`。
    """
    cfg = config or DEFAULT_CONFIG
    model = model_alias or cfg.summary_model

    transcript = _messages_to_transcript(messages)

    user_text = f"## 需要总结的对话\n\n{transcript}"
    if problem_record:
        tags_cn = [_TAG_CN.get(t, t) for t in (problem_record.tags or [])]
        user_text = (
            f"## 题目信息\n"
            f"- 标题：{problem_record.title}\n"
            f"- 难度：{problem_record.difficulty}\n"
            f"- 知识点：{', '.join(tags_cn) if tags_cn else '未知'}\n"
            f"- 最终判题结果：{problem_record.verdict}\n"
            f"- 辅导提示等级：L{problem_record.hint_level_reached}\n\n"
            f"{user_text}"
        )

    try:
        from code_tutor_agent.config import get_llm

        llm = get_llm(model, temperature=0.3, max_tokens=cfg.summary_max_tokens)
        resp = await llm.ainvoke([
            ("system", _SUMMARY_SYSTEM_PROMPT),
            ("human", user_text),
        ])
        content = resp.content if hasattr(resp, "content") else str(resp)
        result = content.strip()
        if result:
            logger.info("Async summary generated: %d chars", len(result))
            return result
    except Exception as exc:
        logger.warning("Async LLM summary failed: %s — falling back", exc)

    return _rule_based_summary(messages, problem_record)


def _rule_based_summary(
    messages: list[Message],
    problem_record: ProblemAttemptRecord | None = None,
) -> str:
    """基于规则的降级摘要（LLM 不可用时自动降级）。

    提取：题目信息 + 最后几条用户消息的关键片段。
    虽然不如 LLM 摘要精确，但远好于直接丢弃。

    Args:
        messages: 对话消息列表。
        problem_record: 题目记录。

    Returns:
        规则生成的摘要文本。
    """
    parts: list[str] = []

    # 题目信息
    if problem_record:
        verdict_label = {"AC": "通过", "WA": "未通过", "ABANDON": "放弃"}.get(
            problem_record.verdict, problem_record.verdict,
        )
        tags_cn = [_TAG_CN.get(t, t) for t in (problem_record.tags or [])]
        parts.append(
            f"题目「{problem_record.title}」"
            f"（{problem_record.difficulty}，{', '.join(tags_cn) if tags_cn else '未知'}）"
            f" — {verdict_label}，提示到 L{problem_record.hint_level_reached}"
        )

    # 提取最近 3 条用户消息的要旨
    user_msgs = [m for m in messages if _to_msg_dict(m)["role"] == "user"]
    if user_msgs:
        parts.append("用户关注点：")
        for msg in user_msgs[-3:]:
            content = _to_msg_dict(msg)["content"]
            snippet = content[:80] + ("..." if len(content) > 80 else "")
            parts.append(f"  · {snippet}")

    return "\n".join(parts) if parts else "（无可用摘要）"


# ═══════════════════════════════════════════════════════════════════
#  跨题上下文构建（Agent 模式专用）
# ═══════════════════════════════════════════════════════════════════


def build_cross_problem_context(
    problem_history: list[ProblemAttemptRecord],
    config: ContextConfig | None = None,
) -> str:
    """从已完成的题目列表中构建跨题上下文摘要。

    Agent 模式换题时调用，将结构化记录转为自然语言摘要。
    不保留逐轮对话原文，只提取每道题的关键结果和薄弱模式。

    示例输出：
        ## 已完成题目
        1. 两数之和（easy，双指针）— ✅通过，提示到 L2
        2. 三数之和（medium，双指针）— ❌未通过，提示到 L3

        **需要注意的方向**：双指针

    Args:
        problem_history: 已完成题目的记录列表（按时间序）。
        config: 上下文配置。

    Returns:
        格式化的跨题上下文文本。history 为空时返回空字符串。
    """
    if not problem_history:
        return ""

    lines = ["## 已完成的题目"]
    for i, record in enumerate(problem_history, 1):
        verdict_label = {"AC": "✅通过", "WA": "❌未通过", "ABANDON": "⏭放弃"}.get(
            record.verdict, record.verdict,
        )
        tags_cn = [_TAG_CN.get(t, t) for t in (record.tags or [])]
        tags_str = "、".join(tags_cn) if tags_cn else "未知"
        lines.append(
            f"{i}. {record.title}（{record.difficulty}，{tags_str}）— {verdict_label}"
            f"，提示到 L{record.hint_level_reached}"
        )

    # 检测重复出错的薄弱标签
    weak_tags_str = _detect_weak_patterns(problem_history)
    if weak_tags_str:
        lines.append(f"\n**需要注意的方向**：{weak_tags_str}")

    return "\n".join(lines)


def _detect_weak_patterns(history: list[ProblemAttemptRecord]) -> str:
    """从题目历史中检测重复出错的薄弱知识点。

    策略：
        - 统计每个 tag 的 WA/ABANDON 次数
        - 失败 ≥ 2 次 → 标记为薄弱知识点
        - 返回中文标签名称，以顿号分隔

    Args:
        history: 题目记录列表。

    Returns:
        薄弱标签的中文描述，如 "双指针、边界条件"。
        无模式时返回空字符串。
    """
    tag_failures: Counter[str] = Counter()
    for record in history:
        if record.verdict in ("WA", "ABANDON"):
            for tag in (record.tags or []):
                tag_failures[tag] += 1

    weak = [tag for tag, count in tag_failures.items() if count >= 2]
    if not weak:
        return ""

    cn_names = [_TAG_CN.get(t, t) for t in weak]
    return "、".join(cn_names)
