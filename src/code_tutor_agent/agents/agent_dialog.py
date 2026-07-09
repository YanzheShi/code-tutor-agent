"""Agent dialog agent — LLM-driven conversation to determine user preferences.

Flow:
    1. SSE handler receives user message → appends to history
    2. Calls ``stream_dialog_response()`` (async generator)
       - First: non-streaming ``analyze_user_intent()`` → reliable structured output
       - Then: streams the ``next_message`` via a real streaming LLM call
    3. After streaming, caller reads ``DialogIntent`` for routing decisions
"""

from __future__ import annotations

import json
import logging
import re
from typing import AsyncGenerator

from pydantic import BaseModel, Field

from code_tutor_agent.config import get_llm
from code_tutor_agent.schemas.state import Message

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  Structured output model
# ──────────────────────────────────────────────


class DialogIntent(BaseModel):
    """Parsed structured output from the LLM."""

    topic: str = Field(default="", description="确定的知识点")
    difficulty: str = Field(default="", description="easy/medium/hard")
    is_ready: bool = Field(default=False, description="是否可以出题了")
    next_message: str = Field(default="", description="给用户的下一轮消息")


# ──────────────────────────────────────────────
#  Prompts
# ──────────────────────────────────────────────

AGENT_DIALOG_SYSTEM = """你是 AI 编程导师的对话助手。你的任务是通过多轮对话了解用户想练习什么类型的算法题。

## 目标
1. **知识点（topic）**：如 "数组"、"双指针"、"动态规划" 等
2. **难度（difficulty）**：easy / medium / hard

## 对话策略
- 第一轮：友好地询问用户想练什么类型，给一些选项引导
- 后续轮次：根据回答深入追问，逐步缩小范围
  - 用户说 "数组" → "数组的哪方面？遍历、排序、双指针、还是滑动窗口？"
  - 用户说 "随便" → 推荐几个方向让用户选
  - 用户 topic 明确后 → 追问难度："你想从 Easy 开始热身，还是直接挑战 Medium？"
  - 用户说 "难度无所谓" → "那我建议从 Medium 开始，既有挑战又不会太难"
  - 用户主动说 "给我出题" 且 topic+难度都明确 → 直接出题
- 用中文交流，语气友好、鼓励
- 不要急于一次确定所有信息，享受对话过程

## topic 识别规则
- 需要从对话中提取具体的算法知识点（不是泛泛的"算法"、"编程"、"题目"）
- 如果用户说了多个知识点，取最后一个明确的
- 如果用户说"随便"或"都行"，topic 保持空串，给选项引导

## 何时标记 is_ready=true
只有当 topic 和 difficulty 都足够明确，可以直接出题时才设为 true。
确认清楚："topic 数组 + difficulty easy" 这样的才算 ready。

## 输出 JSON
```json
{
  "topic": "确定的知识点或空字符串",
  "difficulty": "easy/medium/hard 或空字符串",
  "is_ready": true/false,
  "next_message": "给用户的下一轮对话消息（仅文本，不含JSON）"
}
```"""

CHAT_STREAM_SYSTEM = """你是 AI 编程导师，你的任务是通过对话了解用户想练习什么类型的算法题。

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
#  Core functions
# ──────────────────────────────────────────────


def _build_transcript(history: list[Message]) -> str:
    lines = []
    for msg in history:
        prefix = "用户" if msg.role == "user" else "AI导师"
        lines.append(f"{prefix}: {msg.content}")
    return "\n".join(lines)


def analyze_user_intent(
    history: list[Message],
    model_alias: str = "agnes",
) -> DialogIntent:
    """Synchronous structured analysis — reliable JSON via with_structured_output.

    Args:
        history: Conversation history.
        model_alias: LLM alias.

    Returns:
        DialogIntent with determined fields.
    """
    logger.info("▶ analyze_user_intent() — %d messages", len(history))
    transcript = _build_transcript(history)

    try:
        llm = get_llm(model_alias, temperature=0.7)
        structured_llm = llm.with_structured_output(DialogIntent)
        user_prompt = f"## 对话历史\n\n{transcript}\n\n请分析用户的意图。"

        result: DialogIntent = structured_llm.invoke([
            ("system", AGENT_DIALOG_SYSTEM),
            ("human", user_prompt),
        ])
        logger.info("intent → topic=%s diff=%s ready=%s",
                     result.topic or "?", result.difficulty or "?", result.is_ready)
        return result
    except Exception as exc:
        logger.warning("LLM structured output failed: %s", exc)
        return DialogIntent(
            next_message="我没理解清楚，能再详细说说你想练什么类型的题吗？",
        )


async def stream_dialog_response(
    history: list[Message],
    model_alias: str = "agnes-stream",
) -> AsyncGenerator[str, None]:
    """Stream the AI's dialog response to the user.

    Uses the structured analysis result to determine intent,
    then streams the conversation naturally via a separate LLM call.

    Args:
        history: Conversation history.
        model_alias: Streaming LLM alias.

    Yields:
        Tokens of the natural language response.
    """
    transcript = _build_transcript(history)

    # Build a natural conversational prompt (no JSON output)
    system = CHAT_STREAM_SYSTEM
    user_prompt = f"## 对话历史\n\n{transcript}\n\n## 当前消息\n{history[-1].content if history else ''}\n\n请回复。"

    try:
        llm = get_llm(model_alias, temperature=0.7)
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
#  Message builders
# ──────────────────────────────────────────────


def build_initial_message() -> Message:
    return Message(
        role="tutor",
        content="你好！我是你的 AI 编程导师 🧑‍🏫 今天想练习什么类型的算法题？"
                "比如数组、链表、双指针、动态规划……你对哪个方向感兴趣？",
    )


def build_ready_message(topic: str, difficulty: str) -> Message:
    diff_label = {"easy": "简单", "medium": "中等", "hard": "困难"}.get(difficulty, difficulty)
    return Message(
        role="tutor",
        content=f"好的！我来为你准备一道 **{topic}** 方向、**{diff_label}** 难度的题。"
                "请稍等，马上就好 🚀",
    )