"""Agent 对话 Agent — LLM 驱动的对话，用于确定用户偏好。

流程：
    1. SSE 处理器接收用户消息 → 追加到历史
    2. 调用 ``stream_dialog_response()``（异步生成器）
       - 先：非流式 ``analyze_user_intent()`` → 可靠的结构化输出
       - 后：通过真正的流式 LLM 调用，流式输出 ``next_message``
    3. 流式输出结束后，调用方读取 ``DialogIntent`` 进行路由决策
"""

from __future__ import annotations

import json
import logging
import re
from typing import AsyncGenerator, Literal

from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage

from code_tutor_agent.config import get_llm
from code_tutor_agent.schemas.state import Message

logger = logging.getLogger(__name__)

# ── Tag enum → 中文名称映射（与 planner.py 的 _TAG_TO_TOPIC 一致）──
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

# ──────────────────────────────────────────────
#  结构化输出模型
# ──────────────────────────────────────────────


class DialogIntent(BaseModel):
    """Parsed structured output from the LLM."""

    topic: str = Field(default="", description="确定的知识点")
    difficulty: str = Field(default="", description="easy/medium/hard")
    is_ready: bool = Field(default=False, description="是否可以出题了")
    next_message: str = Field(default="", description="给用户的下一轮消息")
    # —— 工具调用扩展（由 agent 工具循环填充，LLM 结构化输出不会生成这些）——
    source: Literal["generated", "leetcode"] = Field(default="generated", description="题目来源")
    leetcode_url: str = Field(default="", description="source==leetcode 时记录原始链接（解析收口到 generator_node，此处只传 URL）")


# ──────────────────────────────────────────────
#  Prompt 模板
# ──────────────────────────────────────────────

AGENT_DIALOG_SYSTEM = """你是 AI 编程导师的对话助手。你的任务是通过多轮对话了解用户想练习什么类型的算法题。

## 目标
1. **知识点（topic）**：如 "数组"、"双指针"、"动态规划" 等
2. **难度（difficulty）**：easy / medium / hard

{profile_section}

## 对话策略
- 第一轮：友好地询问用户想练什么类型，给一些选项引导
- 后续轮次：根据回答深入追问，逐步缩小范围
  - 用户说 "数组" → "数组的哪方面？遍历、排序、双指针、还是滑动窗口？"
  - 用户说 "动态规划" 或 "DP" → topic 直接设为 "动态规划"，追问难度
  - 用户说 "随便" 或 "随机出题" → 这是关键场景：
    * 如果对话刚开始（1-2 轮），推荐几个方向让用户选
    * 如果用户已经 2 轮以上表示无偏好，不要再追问！直接从用户画像弱项中选一个 topic，难度默认 medium，告知用户你的选择并标记 is_ready=true
  - 用户说 "给我出题" 或 "出题吧" → 若 topic 已明确则追问难度，若 topic+难度都明确则 is_ready=true
  - 用户 topic 明确后 → 追问难度："你想从 Easy 开始热身，还是直接挑战 Medium？"
  - 用户说 "难度无所谓" → 难度设为 medium，话题已明确则直接标记 is_ready=true
  - 用户主动指定 topic 和 difficulty（如 "简单动态规划"、"中等难度的数组题"）→ 直接设置 is_ready=true
- 用中文交流，语气友好、鼓励
- 最多 3 轮对话就应该能确定 topic 和 difficulty，不要陷入反复追问

## topic 识别规则
- 需要从对话中提取具体的算法知识点（不是泛泛的"算法"、"编程"、"题目"）
- 如果用户说了多个知识点，取最后一个明确的
- 如果用户说"随便"或"都行"且已有多轮无偏好对话，从用户画像弱项中选一个，不要留空

## 何时标记 is_ready=true
- topic 和 difficulty 都明确 → is_ready=true
- 用户多次表示无偏好（2+ 轮"随便"）→ 自动选一个 topic+medium 难度 → is_ready=true
- 用户明确说"出题吧"/"给我出题"且 topic 和 difficulty 都已确定 → is_ready=true

## LeetCode 链接处理
- 只要用户消息里出现 LeetCode 链接（leetcode.com 或 leetcode.cn 的 /problems/xxx），即视为用户想做这道具体题。
- 不要自己猜测题号或标题，也不要追问"先讲思路还是先给描述"——直接给出友好的确认回复即可，后端会自动识别链接并导入该题。

## 输出 JSON
```json
{{
  "topic": "确定的知识点或空字符串",
  "difficulty": "easy/medium/hard 或空字符串",
  "is_ready": true/false,
  "next_message": "给用户的下一轮对话消息（仅文本，不含JSON）"
}}
```"""

CHAT_STREAM_SYSTEM = """你是 AI 编程导师，你的任务是通过对话了解用户想练习什么类型的算法题。

{profile_section}

## 对话策略
- 第一轮：友好地询问用户想练什么类型
- 后续轮次：根据回答深入追问
  - 用户说 "数组" → "数组的哪方面？遍历、排序、双指针、还是滑动窗口？"
  - 用户说 "链表" → "链表的反转、是否有环、还是合并有序链表？"
  - 用户说 "动态规划" → "背包问题、最长子序列、还是股票买卖系列？"
  - 用户说 "树" → "二叉树遍历、二叉搜索树、还是最近公共祖先？"
  - 用户确定 topic 后 → 追问难度："你想从 Easy 开始热身，还是挑战 Medium？"
  - 用户说 "随便" → "那我推荐几个方向：数组的双指针、链表的反转、二叉树遍历，你感兴趣哪个？"
- 用中文交流，语气友好、鼓励
- 不要急于一次确定所有信息

## 确定 topic 和 difficulty 的规则
- 只有 topic 和 difficulty 都明确了才能说准备好了
- topic 没明确 → 追问具体方向
- topic 明确了但 difficulty 没问 → 追问难度
- 都明确了 → 告诉用户"好的，我来为你准备一道..."

回复控制在 200 字以内。不要输出 JSON，只输出自然语言。"""


# ──────────────────────────────────────────────
#  核心函数
# ──────────────────────────────────────────────


def _build_profile_summary() -> str:
    """Build a Chinese text summary of the user's profile weaknesses.

    Reads the v2 per-tag profile and formats weak/forgotten/unstable tags
    for injection into dialog prompts. Returns empty string if no profile.
    """
    try:
        from code_tutor_agent.db.database import get_user_profile_v2, get_profile
    except Exception:
        logger.debug("Cannot import profile functions")
        return ""

    # Try v2 per-tag profile first
    try:
        profile = get_user_profile_v2()
    except Exception:
        profile = None

    if not isinstance(profile, dict):
        return ""

    prof: dict = profile.get("prof") or {}
    forget: dict = profile.get("forget") or {}
    stab: dict = profile.get("stab") or {}

    # Build scored weaknesses
    weak_tags: list[tuple[str, str, str]] = []  # (tag, cn_name, reason)
    for tag, p in prof.items():
        try:
            p = float(p)
        except (TypeError, ValueError):
            continue
        cn = _TAG_CN.get(tag, tag)
        if p < 0.3:
            weak_tags.append((tag, cn, f"熟练度很低(prof={p:.2f})"))
        elif p < 0.5:
            weak_tags.append((tag, cn, f"不够熟练(prof={p:.2f})"))
        f = forget.get(tag) or {}
        decay = float(f.get("decay", 1.0))
        if decay < 0.5:
            weak_tags.append((tag, cn, f"遗忘严重(decay={decay:.2f})"))
        s = stab.get(tag) or {}
        variance = float(s.get("variance", 0.0))
        if variance > 0.5:
            weak_tags.append((tag, cn, f"稳定性差(variance={variance:.2f})"))

    if not weak_tags:
        # Try legacy profile
        try:
            legacy = get_profile()
            if legacy and legacy.attempts > 0:
                return (
                    "## 用户画像\n"
                    f"总练习次数: {legacy.attempts} 次\n"
                    f"综合熟练度: {legacy.proficiency:.0%}\n"
                    "用户有一定练习经验，可引导其选择感兴趣的 topic。\n"
                )
        except Exception:
            pass
        return ""

    # Deduplicate and pick top 5
    seen: set[str] = set()
    unique: list[str] = []
    for _, cn, reason in weak_tags:
        if cn not in seen:
            seen.add(cn)
            unique.append(f"- **{cn}**：{reason}")
            if len(unique) >= 5:
                break

    if not unique:
        return ""

    parts: list[str] = [
        "## 用户画像（弱项分析）",
        "以下是根据历史练习数据识别出的用户薄弱知识点，在导引话题时可优先推荐这些方向：",
        *unique,
        "在对话中，如果用户说\"随便\"或不确定，应优先推荐上述弱项中最相关的方向。",
    ]
    return "\n".join(parts)


def _build_memory_summary() -> str:
    """加载跨会话语义记忆(偏好/行为习惯),渲染为注入块。

    与 _build_profile_summary 互补:画像=规则统计的定量层,
    记忆=LLM 语义抽取的定性层(见 docs/agent-memory-design.md)。
    无记忆/加载失败 → 空串,不影响对话。
    """
    try:
        from code_tutor_agent.memory import render_memory_summary
        return render_memory_summary()
    except Exception:
        logger.debug("Cannot render memory summary")
        return ""


def _to_msg_dict(msg) -> dict:
    """Normalize a Message object or plain dict into a consistent dict with 'role' and 'content'."""
    if isinstance(msg, dict):
        return {"role": msg.get("role", "tutor"), "content": msg.get("content", "")}
    if hasattr(msg, "role") and hasattr(msg, "content"):
        return {"role": msg.role, "content": msg.content}
    # Last resort: treat as string
    return {"role": "tutor", "content": str(msg)}


def _build_transcript(history: list[Message], context_summary: str | None = None) -> str:
    """构建对话 transcript，带 token 预算管理（滑动窗口 + 摘要压缩）。

    当对话 token 数超过预算阈值时，自动对旧消息应用滑动窗口裁剪；
    如果提供了 context_summary（之前的压缩摘要），会拼接到 transcript 开头。

    Args:
        history: 对话历史消息列表。
        context_summary: 压缩后的旧消息摘要（SessionState.context_summary）。

    Returns:
        格式化后的 transcript，保证 token 不超过预算。
    """
    from code_tutor_agent.context_manager import build_transcript_with_budget, DEFAULT_CONFIG
    return build_transcript_with_budget(history, context_summary, DEFAULT_CONFIG)


def _fallback_parse_intent(transcript: str, profile_summary: str) -> DialogIntent:
    """Fallback when structured output fails: raw LLM call with manual JSON parse.

    Tries:
        1. Raw LLM + JSON.parse
        2. Regex extraction from transcript if LLM also fails
    """
    last_user_msg = ""
    for line in reversed(transcript.split("\n")):
        if line.startswith("用户:"):
            last_user_msg = line[3:].strip()
            break

    # 先用 LLM 非结构化请求提取意图
    try:
        llm = get_llm(purpose="dialog")
        prompt = (
            "你是 AI 编程导师。根据以下对话，提取用户的选题意图。\n\n"
            f"## 对话历史\n{transcript}\n\n"
            "请严格输出如下 JSON（不要输出其他内容）：\n"
            '{"topic": "知识点或空", "difficulty": "easy/medium/hard或空", "is_ready": false, "next_message": "你的回复"}\n\n'
            "规则：\n"
            '- 若用户说了具体方向（数组/链表/动态规划/二叉树等），topic 填入该方向\n'
            '- 若用户没有明确 topic，topic 留空 ，引导追问\n'
            '- 若 topic + difficulty 都明确，is_ready=true\n'
            '- next_message 是给用户的自然语言回复\n'
        )
        resp = llm.invoke([("human", prompt)])
        text = resp.content if hasattr(resp, "content") else str(resp)
        # 提取 JSON
        json_match = re.search(r'\{[\s\S]*"topic"[\s\S]*"next_message"[\s\S]*\}', text)
        if json_match:
            data = json.loads(json_match.group())
            return DialogIntent(**data)
    except Exception as exc:
        logger.warning("Fallback LLM parse failed: %s", exc)

    # 最终兜底：用正则从用户消息中提取 topic
    known_topics = [
        "动态规划", "数组", "链表", "二叉树", "字符串", "回溯", "贪心",
        "双指针", "滑动窗口", "二分查找", "栈", "队列", "哈希表", "排序",
        "递归", "前缀和", "位运算", "图", "堆", "并查集",
    ]
    found_topic = ""
    for topic in known_topics:
        if topic in last_user_msg or topic in transcript:
            found_topic = topic
            break

    if found_topic:
        # 检测难度
        diff = ""
        if any(w in last_user_msg for w in ["简单", "easy", "容易", "入门"]):
            diff = "easy"
        elif any(w in last_user_msg for w in ["困难", "hard", "难"]):
            diff = "hard"
        elif any(w in last_user_msg for w in ["中等", "medium"]):
            diff = "medium"

        if diff:
            _diff_label = {"easy": "简单", "medium": "中等", "hard": "困难"}.get(diff, diff)
            return DialogIntent(
                topic=found_topic,
                difficulty=diff,
                is_ready=True,
                next_message=f"好的！我来为你准备一道 **{found_topic}** 方向、**{_diff_label}** 难度的题。请稍等 🚀",
            )
        else:
            return DialogIntent(
                topic=found_topic,
                is_ready=False,
                next_message=f"明白了，你想练习 **{found_topic}** 方向！你想从 Easy（简单）开始热身，还是直接挑战 Medium（中等）？",
            )

    logger.warning("All fallback methods failed — returning generic prompt")
    return DialogIntent(
        next_message="我没理解清楚，能再详细说说你想练什么类型的题吗？比如数组、动态规划、链表……",
    )


def _pick_auto_topic(profile_summary: str) -> str:
    """从用户画像中提取弱项 topic，无画像时随机选。"""
    # 尝试从画像中提取弱项名称
    weak_names: list[str] = []
    for line in profile_summary.split("\n"):
        stripped = line.strip()
        if stripped.startswith("- **") and "**：" in stripped:
            name = stripped.split("**")[1]
            weak_names.append(name)

    if weak_names:
        return weak_names[0]

    # 无画像 → 随机选一个常用方向
    fallback = ["数组", "双指针", "链表", "动态规划", "滑动窗口", "二分查找", "字符串"]
    import random
    return random.choice(fallback)


def _extract_leetcode_url(history: list[Message]) -> str | None:
    """从「最近一条用户消息」里找出 LeetCode 题目链接（若有）。

    只扫最后一条用户消息：URL 触发导入的语义是"当前这条消息想做这道具体题"，
    历史里的旧链接不应在后续对话中被反复重新导入（避免解析失败后，下一轮对话
    又触发同一失败链接，形成死循环）。
    """
    pattern = re.compile(r"https?://(?:www\.)?(?:leetcode\.(?:com|cn))/problems/([^/\s?#]+)")
    for m in reversed(history):
        msg = _to_msg_dict(m)
        if msg.get("role") != "user":
            continue
        mm = pattern.search(msg.get("content", "") or "")
        return mm.group(0) if mm else None
    return None


async def analyze_user_intent(
    history: list[Message],
    purpose: str = "dialog",
    context_summary: str | None = None,
) -> DialogIntent:
    """Structured analysis with an optional tool-calling loop.

    If the user pasted a LeetCode link, the intent is resolved immediately as
    ``source="leetcode"`` + ``leetcode_url`` (is_ready=True). The actual
    parsing/fetching is consolidated in the generation package (generator path
    A) — this layer only forwards the raw URL, never pre-parses.

    Falls back to raw LLM + regex parsing if structured output fails.

    Args:
        history: Conversation history.
        purpose: 模型用途（由 config.PURPOSE_CONFIGS 决定具体模型）。
        context_summary: 压缩后的旧消息摘要（来自 SessionState.context_summary）。

    Returns:
        DialogIntent with determined fields.
    """
    logger.info("▶ analyze_user_intent() — %d messages", len(history))

    # ── 提取 LeetCode 链接：直接走导入通道，解析收口到 generator_node ──
    leetcode_url = _extract_leetcode_url(history)
    if leetcode_url:
        return DialogIntent(
            topic="",
            difficulty="",
            is_ready=True,
            source="leetcode",
            leetcode_url=leetcode_url,
            next_message="好的，已识别 LeetCode 题目链接，正在导入并生成，请稍等 🚀",
        )

    # ── 硬守护：检测"随便"循环，避免反复追问 ──
    if not leetcode_url:
        _no_pref_keywords = ["随便", "随机", "都可以", "都行", "无所谓", "你来选", "你定吧"]
        _user_msgs = [m.content for m in history if _to_msg_dict(m).get("role") == "user"]
        _no_pref_count = sum(
            1 for msg in _user_msgs[-3:]  # 只看最近 3 条用户消息
            if any(kw in msg for kw in _no_pref_keywords)
        )
        if _no_pref_count >= 2 and len(history) >= 4:
            # 用户连续 2+ 轮无偏好 → 自动选方向，不再追问
            logger.info("'随便' loop detected — auto-selecting topic")
            profile_summary = _build_profile_summary()
            auto_topic = _pick_auto_topic(profile_summary)
            return DialogIntent(
                topic=auto_topic,
                difficulty="medium",
                is_ready=True,
                next_message=(
                    f"好的，我看到你还没确定方向，那我帮你选一道 **{auto_topic}** 的题吧！"
                    "难度中等，不会太简单也不会太难，马上为你生成 🚀"
                ),
            )

    transcript = _build_transcript(history, context_summary)
    profile_summary = _build_profile_summary()
    memory_summary = _build_memory_summary()
    system_prompt = AGENT_DIALOG_SYSTEM.format(profile_section=profile_summary or "")
    user_prompt = (
        f"## 对话历史\n\n{transcript}\n\n"
        + (f"## 用户画像信息\n\n{profile_summary}\n\n" if profile_summary else "")
        + (f"{memory_summary}\n\n" if memory_summary else "")
        + "请分析用户的意图。"
    )

    try:
        llm = get_llm(purpose=purpose, temperature=0.7)
    except Exception as exc:
        # get_llm 失败（配置/网络）→ 直接走兜底，避免工具循环也崩
        logger.warning("get_llm failed, using fallback: %s", exc)
        return _fallback_parse_intent(transcript, profile_summary)

    # ── 结构化意图判定（LeetCode 已在上面短链返回，此处只处理非 LeetCode 意图）──
    try:
        structured_llm = llm.with_structured_output(DialogIntent)
        intent = structured_llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ])
    except Exception as exc:
        logger.error("LLM structured output failed, fallback: %s", exc)
        intent = _fallback_parse_intent(transcript, profile_summary)

    logger.info("intent → topic=%s diff=%s ready=%s source=%s",
                intent.topic or "?", intent.difficulty or "?", intent.is_ready, intent.source)
    return intent


async def stream_dialog_response(
    history: list[Message],
    purpose: str = "dialog-stream",
    context_summary: str | None = None,
) -> AsyncGenerator[str, None]:
    """Stream the AI's dialog response to the user.

    Uses the structured analysis result to determine intent,
    then streams the conversation naturally via a separate LLM call.

    Args:
        history: Conversation history.
        purpose: 模型用途（由 config.PURPOSE_CONFIGS 决定具体模型，默认流式）。
        context_summary: 压缩后的旧消息摘要（来自 SessionState.context_summary）。

    Yields:
        Tokens of the natural language response.
    """
    transcript = _build_transcript(history, context_summary)
    profile_summary = _build_profile_summary()
    memory_summary = _build_memory_summary()

    # 构建自然对话 prompt（不输出 JSON）
    system = CHAT_STREAM_SYSTEM.format(profile_section=profile_summary or "")
    user_prompt = (
        f"## 对话历史\n\n{transcript}\n\n"
        + (f"## 用户画像信息\n\n{profile_summary}\n\n" if profile_summary else "")
        + (f"{memory_summary}\n\n" if memory_summary else "")
        + f"## 当前消息\n{history[-1].content if history else ''}\n\n请回复。"
    )

    try:
        llm = get_llm(purpose=purpose, temperature=0.7)
        async for chunk in llm.astream([
            ("system", system),
            ("human", user_prompt),
        ]):
            token = chunk.content if hasattr(chunk, "content") else str(chunk)
            if token:
                yield token
    except Exception as exc:
        logger.warning("Stream chat LLM failed: %s", exc)
        yield "抱歉，我现在无法回答。能再详细说说吗？"


# ──────────────────────────────────────────────
#  消息构建
# ──────────────────────────────────────────────


def build_initial_message() -> Message:
    profile_summary = _build_profile_summary()
    if profile_summary:
        # 从画像中提取弱项列表，拼接到欢迎消息中
        weak_names: list[str] = []
        for line in profile_summary.split("\n"):
            stripped = line.strip()
            if stripped.startswith("- **") and "**：" in stripped:
                name = stripped.split("**")[1]
                weak_names.append(name)
        if weak_names:
            suggestions = "、".join(weak_names[:3])
            return Message(
                role="tutor",
                content=f"你好！我是你的 AI 编程导师 🧑‍🏫\n\n"
                        f"我注意到你的薄弱项有：**{suggestions}**。"
                        f"今天想针对这些方向练习，还是试试别的知识点？\n\n"
                        f"也可以直接把一道 LeetCode 题目链接发给我，我们一起把它啃下来 👇",
            )
    return Message(
        role="tutor",
        content="你好！我是你的 AI 编程导师 🧑‍🏫 今天想练习什么类型的算法题？"
                "比如数组、链表、双指针、动态规划……你对哪个方向感兴趣？\n\n"
                "或者直接把一道 LeetCode 题目链接发给我也可以哦～",
    )


def build_ready_message(topic: str, difficulty: str) -> Message:
    diff_label = {"easy": "简单", "medium": "中等", "hard": "困难"}.get(difficulty, difficulty)
    return Message(
        role="tutor",
        content=f"好的！我来为你准备一道 **{topic}** 方向、**{diff_label}** 难度的题。"
                "请稍等，马上就好 🚀",
    )