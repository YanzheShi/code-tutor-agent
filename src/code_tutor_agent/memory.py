"""Agent Memory —— 语义抽取式用户记忆(定性层)。

与 profile/ 定量画像互补:画像由规则从判题结构化事件中抽取,
本模块由 LLM 从自然语言对话中抽取【稳定的】偏好与行为习惯。

- 写入:critic_node 在 episode 终结(AC/ABANDON 非去重分支)时调
  ``schedule_extraction(state)``;实际抽取在后台线程异步执行,
  永不阻塞主链路,任何异常只记日志。
- 存储:SQLite ``profiles`` 表一行 JSON(user_id='__memory__'),
  单 writer 纪律——只有本模块写。
- 读取:``render_memory_summary()`` 供 agent_dialog 注入 prompt。

设计文档:docs/agent-memory-design.md
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── 容量与门控常量(schema 固定 → 天然防膨胀)──
PREF_KEYS = ("language", "hint_style", "difficulty_preference", "communication", "goals")
BEHAVIOR_CAP = 8
OBSERVATIONS_CAP = 5
MIN_USER_MSGS = 3          # 增量中用户消息不足此数 → 跳过抽取(纯刷题零交流)
MEMORY_PURPOSE = "memory-extract"

# ── 串行化:后台线程读改写同一行 SQLite ──
_WRITE_LOCK = threading.Lock()


# ──────────────────────────────────────────────
#  Schema
# ──────────────────────────────────────────────


class MemoryExtraction(BaseModel):
    """LLM 抽取输出。unchanged=True 时其余字段忽略。"""

    unchanged: bool = Field(default=True, description="无新稳定信号时为 True")
    preferences: dict = Field(
        default_factory=dict,
        description="仅允许 language/hint_style/difficulty_preference/communication/goals 五键",
    )
    behavior: list[str] = Field(default_factory=list, description="≤8 条,整表重写")
    observations: list[str] = Field(default_factory=list, description="≤5 条,整表重写")


def empty_memory() -> dict:
    return {
        "schema_version": "memory@1",
        "preferences": {},
        "behavior": [],
        "observations": [],
        "meta": {
            "updated_at": "",
            "watermark": {"session_id": "", "count": 0},
        },
    }


def load_memory() -> dict:
    """读取记忆,坏数据/缺失 → 空记忆兜底(不抛错)。"""
    from code_tutor_agent.db.database import get_user_memory
    data = get_user_memory()
    if not isinstance(data, dict) or not data:
        return empty_memory()
    base = empty_memory()
    base["preferences"] = dict(data.get("preferences") or {})
    base["behavior"] = list(data.get("behavior") or [])[:BEHAVIOR_CAP]
    base["observations"] = list(data.get("observations") or [])[:OBSERVATIONS_CAP]
    meta = data.get("meta") or {}
    base["meta"]["updated_at"] = meta.get("updated_at", "")
    wm = meta.get("watermark") or {}
    base["meta"]["watermark"] = {
        "session_id": wm.get("session_id", ""),
        "count": int(wm.get("count", 0) or 0),
    }
    return base


def save_memory(memory: dict) -> None:
    from code_tutor_agent.db.database import save_user_memory
    save_user_memory(memory)


# ──────────────────────────────────────────────
#  Watermark:增量对话切片
# ──────────────────────────────────────────────


def _delta_start(memory: dict, session_id: str, msg_count: int) -> int:
    """计算 tutor_messages 的增量起点。

    - 新会话 → 0
    - 消息数变短(普通模式换题清空)→ 0
    - 否则 → 上次水位
    """
    wm = memory.get("meta", {}).get("watermark", {})
    if wm.get("session_id") != session_id:
        return 0
    prev = int(wm.get("count", 0) or 0)
    if msg_count < prev:
        return 0
    return prev


def _format_transcript(delta: list[dict]) -> str:
    """增量消息 → 对话文本,只保留 user/tutor(过滤题面/welcome 等 system 内容)。"""
    lines = []
    for m in delta:
        role = m.get("role", "")
        content = (m.get("content") or "").strip()
        if role in ("user", "tutor") and content:
            lines.append(f"{'用户' if role == 'user' else '导师'}: {content}")
    return "\n".join(lines)


# ──────────────────────────────────────────────
#  抽取 prompt
# ──────────────────────────────────────────────

_EXTRACT_SYSTEM = """你是刷题导师系统的记忆抽取器。你的唯一任务:从用户最近的一段学习对话(增量)中,
识别【稳定的】用户偏好和行为习惯,并更新用户记忆。

铁律:
1. 只记稳定信号——会反复出现的偏好/习惯。"今天想做 DP"不算,"偏好中等难度"算。
2. 宁缺毋滥——没有新信号就输出 unchanged=true。多数 episode 应该什么都不记。
3. preferences 只允许 5 个键:language / hint_style / difficulty_preference /
   communication / goals。不要发明新键。
4. behavior ≤ 8 条,observations ≤ 5 条。重写时合并重复、删除已被新证据推翻的旧条目。
5. 不记录题目内容、代码细节、一次性事件、解题过程。
6. 用中文、简短的陈述句。"""

_EXTRACT_USER = """## 当前记忆
{memory_json}

## 增量对话(最近一段)
{transcript}

## 本题结果
verdict={verdict} topic={topic} difficulty={difficulty} hint_level={hint_level}

请输出更新后的记忆。"""


def _call_llm(memory: dict, transcript: str, payload: dict) -> Optional[MemoryExtraction]:
    """一次结构化抽取调用;失败返回 None(按 unchanged 处理)。"""
    import json as _json

    from code_tutor_agent.config import get_llm

    compact = {
        "preferences": memory.get("preferences", {}),
        "behavior": memory.get("behavior", []),
        "observations": memory.get("observations", []),
    }
    try:
        llm = get_llm(purpose=MEMORY_PURPOSE)
        structured = llm.with_structured_output(MemoryExtraction)
        return structured.invoke([
            SystemMessage(content=_EXTRACT_SYSTEM),
            HumanMessage(content=_EXTRACT_USER.format(
                memory_json=_json.dumps(compact, ensure_ascii=False),
                transcript=transcript,
                verdict=payload.get("verdict", ""),
                topic=payload.get("topic", ""),
                difficulty=payload.get("difficulty", ""),
                hint_level=payload.get("hint_level", 0),
            )),
        ])
    except Exception as exc:
        logger.warning("memory: LLM extraction failed: %s", exc)
        return None


# ──────────────────────────────────────────────
#  Merge:防御性合并
# ──────────────────────────────────────────────


def _merge(memory: dict, extraction: MemoryExtraction) -> dict:
    """LLM 输出 → 新记忆。preferences 白名单过滤后按键覆盖;
    behavior/observations 采用 LLM 重写结果(它已持有旧记忆),强制截断上限。"""
    new = empty_memory()
    old_prefs = dict(memory.get("preferences") or {})
    new_prefs = {
        k: v for k, v in (extraction.preferences or {}).items()
        if k in PREF_KEYS and v
    }
    new["preferences"] = {**old_prefs, **new_prefs}

    def _clean(items: list | None) -> list[str]:
        return [s for s in (str(x).strip() for x in (items or [])) if s]

    new["behavior"] = _clean(extraction.behavior)[:BEHAVIOR_CAP]
    new["observations"] = _clean(extraction.observations)[:OBSERVATIONS_CAP]
    return new


def _has_signal(extraction: MemoryExtraction) -> bool:
    """unchanged=False 但三个通道全空 → 视为无信号(防误清)。"""
    if extraction.unchanged:
        return False
    prefs = {k: v for k, v in (extraction.preferences or {}).items() if k in PREF_KEYS and v}
    return bool(prefs or extraction.behavior or extraction.observations)


# ──────────────────────────────────────────────
#  入口:critic 调度的异步抽取
# ──────────────────────────────────────────────


def schedule_extraction(state) -> None:
    """critic_node 在 episode 终结时调用。同步快照 state → 后台线程抽取。

    同步快照避免线程读 state 的竞争;启动失败/快照失败都静默降级。
    """
    try:
        msgs = [
            {"role": m.role, "content": m.content}
            for m in (getattr(state, "tutor_messages", None) or [])
        ]
        problem = getattr(state, "problem", None)
        payload = {
            "session_id": getattr(state, "session_id", "") or "",
            "messages": msgs,
            "verdict": getattr(state, "last_verdict", "") or "",
            "topic": getattr(problem, "topic", "") if problem else "",
            "difficulty": getattr(problem, "difficulty", "") if problem else "",
            "hint_level": getattr(state, "hint_level", 0) or 0,
        }
    except Exception as exc:
        logger.warning("memory: failed to snapshot state, skip: %s", exc)
        return
    try:
        threading.Thread(
            target=_run_extraction, args=(payload,),
            daemon=True, name="memory-extract",
        ).start()
    except Exception as exc:
        logger.warning("memory: failed to start extraction thread: %s", exc)


def _run_extraction(payload: dict) -> None:
    """后台线程主流程:切片 → 门控 → LLM → merge → 落库。全程 try/except。"""
    try:
        with _WRITE_LOCK:
            memory = load_memory()
            messages = payload["messages"]
            session_id = payload["session_id"]

            start = _delta_start(memory, session_id, len(messages))
            delta = messages[start:]
            if not delta:
                return

            user_count = sum(1 for m in delta if m.get("role") == "user")
            if user_count < MIN_USER_MSGS:
                _advance_watermark(memory, session_id, len(messages))
                logger.info("memory: gate skip (user msgs %d < %d)", user_count, MIN_USER_MSGS)
                return

            transcript = _format_transcript(delta)
            extraction = _call_llm(memory, transcript, payload)
            if extraction is None or not _has_signal(extraction):
                _advance_watermark(memory, session_id, len(messages))
                logger.info("memory: no stable signal, watermark advanced only")
                return

            new_memory = _merge(memory, extraction)
            new_memory["meta"] = {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "watermark": {"session_id": session_id, "count": len(messages)},
            }
            save_memory(new_memory)
            logger.info(
                "memory: updated — prefs=%d behavior=%d observations=%d",
                len(new_memory["preferences"]), len(new_memory["behavior"]),
                len(new_memory["observations"]),
            )
    except Exception:
        logger.warning("memory: extraction failed", exc_info=True)


def _advance_watermark(memory: dict, session_id: str, count: int) -> None:
    """无内容更新时只推进水位,避免下次重复处理同一段对话。"""
    memory.setdefault("meta", {})
    memory["meta"]["watermark"] = {"session_id": session_id, "count": count}
    save_memory(memory)


# ──────────────────────────────────────────────
#  读取:prompt 注入渲染
# ──────────────────────────────────────────────

_PREF_LABELS = {
    "language": "语言",
    "hint_style": "提示风格",
    "difficulty_preference": "难度倾向",
    "communication": "沟通偏好",
    "goals": "学习目标",
}


def render_memory_summary() -> str:
    """渲染记忆为紧凑文本块,供 agent_dialog 注入;空记忆 → 空串。"""
    try:
        memory = load_memory()
        prefs = memory.get("preferences") or {}
        behavior = memory.get("behavior") or []
        observations = memory.get("observations") or []
        if not prefs and not behavior and not observations:
            return ""

        parts = ["## 用户记忆(跨会话积累,供你参考)"]
        if prefs:
            parts.append("偏好:")
            parts.extend(
                f"- {_PREF_LABELS.get(k, k)}: {v}"
                for k, v in prefs.items() if k in PREF_KEYS and v
            )
        if behavior:
            parts.append("行为:")
            parts.extend(f"- {b}" for b in behavior)
        if observations:
            parts.append("观察:")
            parts.extend(f"- {o}" for o in observations)
        return "\n".join(parts)
    except Exception as exc:
        logger.warning("memory: render failed (ignored): %s", exc)
        return ""
