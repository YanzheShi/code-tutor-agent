"""Agent Memory —— 语义抽取式用户记忆(定性层)。

与 profile/ 定量画像互补:画像由规则从判题结构化事件中抽取,
本模块由 LLM 从自然语言对话中抽取【稳定的】偏好与行为习惯。

- 写入:critic_node 在 episode 终结(AC/ABANDON 非去重分支)时调
  ``schedule_extraction(state)``;实际抽取在后台线程异步执行,
  永不阻塞主链路,任何异常只记日志。
- 存储:PG ``profiles`` 表一行 JSON(user_id='{uid}_memory'),单 writer——只有本模块写。
- 读取:``render_memory_summary()`` 供 agent_dialog 注入 prompt。

Phase 1（memory@2）:
- behavior/observations 由 list[str] 升级为 list[dict],每条带 evidence / source_session
  / first_seen / last_seen / confirm_count / retracted —— 可溯源、可变现、不静默丢失。
- 合并由「整表重写」改为「条目级合并」:旧条目默认保留,只有被显式 retract 才标记作废。
- LLM 失败与「无信号」区分:失败不推进 watermark(下次重试),连续失败超限才放弃该段。

Phase 2（仍 memory@2）:
- 淘汰按优先级:非 retracted > retracted、confirm_count 高 > 低、last_seen 新 > 旧。
- 批次内归一化去重,避免重复条目虚增「确认次数」。
- 作废条目记 retracted_at;`meta.revision` 每次写入递增,`meta.change_log` 保留最近 N 次变更摘要(审计)。

设计文档:docs/agent-memory-design.md
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from datetime import datetime
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

# ── 容量与门控常量 ──
PREF_KEYS = ("language", "hint_style", "difficulty_preference", "communication", "goals")
BEHAVIOR_CAP = 8            # 注入 prompt 的条数（渲染上限）
OBSERVATIONS_CAP = 5
BEHAVIOR_STORE_CAP = 24     # 存储保留条数（含历史 / retracted，防静默丢失）
OBSERVATIONS_STORE_CAP = 16
CHANGE_LOG_CAP = 20         # 审计：保留最近 N 次变更摘要
MIN_USER_MSGS = 3           # 增量中用户消息不足此数 → 跳过抽取(纯刷题零交流)
FAIL_RETRY_LIMIT = 3        # 连续 LLM 失败上限，超过则放弃该段（防无限重试）
SCHEMA_VERSION = "memory@2"
MEMORY_PURPOSE = "memory-extract"

# ── 串行化:后台线程读改写同一行（进程内；多进程在 Phase 3 用 DB 乐观锁兜底）──
_WRITE_LOCK = threading.Lock()


# ──────────────────────────────────────────────
#  Schema
# ──────────────────────────────────────────────


class MemoryItem(BaseModel):
    """一条行为/观察结论 + 证据来源。"""

    text: str = Field(description="一句稳定的行为/观察结论")
    evidence: str = Field(
        default="", description="支持该结论的对话原文片段，必须摘自增量对话，无原文则留空"
    )


class MemoryExtraction(BaseModel):
    """LLM 抽取输出。unchanged=True 时其余字段忽略。"""

    unchanged: bool = Field(default=True, description="无新稳定信号时为 True")
    preferences: dict = Field(
        default_factory=dict,
        description="仅允许 language/hint_style/difficulty_preference/communication/goals 五键",
    )
    behavior: list[MemoryItem] = Field(default_factory=list, description="≤8 条，完整最新列表")
    observations: list[MemoryItem] = Field(default_factory=list, description="≤5 条，完整最新列表")
    retract: list[str] = Field(
        default_factory=list,
        description="要作废的旧条目的 id 或原 text（禁止靠漏写来删除旧条目）",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_items(cls, data):
        """兼容旧调用/旧模型输出：把纯字符串条目包成 {text: ...}。"""
        if isinstance(data, dict):
            for key in ("behavior", "observations"):
                val = data.get(key)
                if isinstance(val, list):
                    data[key] = [
                        {"text": x} if isinstance(x, str) else x for x in val
                    ]
        return data


def empty_memory() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "preferences": {},
        "behavior": [],
        "observations": [],
        "meta": {
            "updated_at": "",
            "watermark": {"session_id": "", "count": 0},
            "fail_count": 0,
            "revision": 0,
            "change_log": [],
        },
    }


# ──────────────────────────────────────────────
#  条目规范化 / 稳定 id
# ──────────────────────────────────────────────

_ID_STRIP = re.compile(r"[\s，。！？、；：,.!?;:\"'（）()\[\]【】]+")


def _item_id(text: str) -> str:
    """归一化文本 → 稳定短 id（同一句话措辞微调也能落到同一 id 的概率更高）。"""
    norm = _ID_STRIP.sub("", (text or "").strip().lower())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def _normalize_item(raw, now: str = "") -> Optional[dict]:
    """str / dict / MemoryItem → 规范 dict；空文本返回 None。"""
    if isinstance(raw, MemoryItem):
        raw = raw.model_dump()
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        return {
            "id": _item_id(text),
            "text": text,
            "evidence": "",
            "source_session": "",
            "first_seen": now,
            "last_seen": now,
            "confirm_count": 1,
            "retracted": False,
            "retracted_at": "",
        }
    if isinstance(raw, dict):
        text = str(raw.get("text") or "").strip()
        if not text:
            return None
        return {
            "id": str(raw.get("id") or _item_id(text)),
            "text": text,
            "evidence": str(raw.get("evidence") or "").strip(),
            "source_session": str(raw.get("source_session") or ""),
            "first_seen": str(raw.get("first_seen") or now),
            "last_seen": str(raw.get("last_seen") or now),
            "confirm_count": int(raw.get("confirm_count") or 1),
            "retracted": bool(raw.get("retracted")),
            "retracted_at": str(raw.get("retracted_at") or ""),
        }
    return None


def _normalize_items(items, now: str = "") -> list[dict]:
    return [x for x in (_normalize_item(i, now) for i in (items or [])) if x]


def _memory_key(user_id: str | None = None) -> str:
    """记忆在 profiles 表里的 user_id key（多用户隔离，P2）。

    - "default"（单用户遗留口径）→ 沿用旧 key "__memory__"，存量数据不丢；
    - 其他用户 → f"{user_id}_memory"。
    """
    if not user_id or user_id == "default":
        from code_tutor_agent.db.database import MEMORY_USER_ID
        return MEMORY_USER_ID
    return f"{user_id}_memory"


def load_memory(user_id: str | None = None) -> dict:
    """读取记忆,坏数据/缺失 → 空记忆兜底(不抛错)。

    老格式（memory@1 的 list[str]）在读取时自动升级为带证据字段的 dict 条目，
    存量数据零丢失。
    """
    from code_tutor_agent.db.database import get_user_memory
    data = get_user_memory(_memory_key(user_id))
    if not isinstance(data, dict) or not data:
        return empty_memory()
    base = empty_memory()
    base["preferences"] = dict(data.get("preferences") or {})
    base["behavior"] = _normalize_items(data.get("behavior"))[:BEHAVIOR_STORE_CAP]
    base["observations"] = _normalize_items(data.get("observations"))[:OBSERVATIONS_STORE_CAP]
    meta = data.get("meta") or {}
    base["meta"]["updated_at"] = meta.get("updated_at", "")
    wm = meta.get("watermark") or {}
    base["meta"]["watermark"] = {
        "session_id": wm.get("session_id", ""),
        "count": int(wm.get("count", 0) or 0),
    }
    base["meta"]["fail_count"] = int(meta.get("fail_count", 0) or 0)
    base["meta"]["revision"] = int(meta.get("revision", 0) or 0)
    cl = meta.get("change_log") or []
    base["meta"]["change_log"] = cl[-CHANGE_LOG_CAP:] if isinstance(cl, list) else []
    return base


def save_memory(memory: dict, user_id: str | None = None) -> None:
    from code_tutor_agent.db.database import save_user_memory
    save_user_memory(memory, user_id=_memory_key(user_id))


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
4. behavior ≤ 8 条,observations ≤ 5 条。你输出的是【完整的最新列表】,不是增量追加。
5. 每条 behavior/observations 必须附 evidence:从"增量对话"里摘一句原文(≤40字),
   作为这条结论的依据。摘不出原文就【不要记】,严禁编造依据。
6. 若某条旧条目被新对话证据推翻,把它的 id 或原 text 放进 retract;
   禁止用"漏写"的方式删除旧条目——漏写会被视为"本条依然有效"。
7. 不记录题目内容、代码细节、一次性事件、解题过程。
8. 用中文、简短的陈述句。"""

_EXTRACT_USER = """## 当前记忆（旧条目的 id / text 可用于 retract）
{memory_json}

## 增量对话(最近一段)
{transcript}

## 本题结果
verdict={verdict} topic={topic} difficulty={difficulty} hint_level={hint_level}

请输出更新后的记忆。"""


def _compact_items(items) -> list[dict]:
    """给 LLM 看的旧条目视图：只留 id/text，省 token 又便于 retract 引用。"""
    out: list[dict] = []
    for it in _normalize_items(items):
        if it.get("retracted"):
            continue
        out.append({"id": it["id"], "text": it["text"]})
    return out


def _call_llm(memory: dict, transcript: str, payload: dict) -> Optional[MemoryExtraction]:
    """一次结构化抽取调用;失败返回 None(与「无信号」区分,由调用方决定是否推进水位)。"""
    import json as _json

    from code_tutor_agent.config import get_llm

    compact = {
        "preferences": memory.get("preferences", {}),
        "behavior": _compact_items(memory.get("behavior")),
        "observations": _compact_items(memory.get("observations")),
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
#  Merge:条目级合并（旧条目默认保留，显式 retract 才作废）
# ──────────────────────────────────────────────


def _keep_rank(item: dict):
    """淘汰优先级：越大越该保留。

    非 retracted > retracted；confirm_count 高 > 低；last_seen 新 > 旧。
    """
    return (
        0 if item.get("retracted") else 1,
        int(item.get("confirm_count", 1) or 1),
        str(item.get("last_seen", "")),
    )


def _merge_section(old_items, new_items, retract_ids, retract_texts, session_id, now, cap) -> list[dict]:
    """把新抽取结果合并进旧条目列表。

    - 旧条目默认保留；命中 retract 的标记 retracted=True + retracted_at（不物理删除）。
    - 新条目按稳定 id 去重：命中「旧条目」才算一次再确认（confirm_count+1）；
      同一批次内的重复条目只保留一条（不虚增确认次数）。
    - 超出存储上限时按 _keep_rank 保留最强的 cap 条（优先淘汰 retracted / 弱证据）。
    """
    old = _normalize_items(old_items, now)
    by_id: dict[str, dict] = {x["id"]: x for x in old}
    old_ids = set(by_id.keys())

    for item in old:
        if item["id"] in retract_ids or item["text"] in retract_texts:
            if not item["retracted"]:
                item["retracted"] = True
                item["retracted_at"] = now

    added_ids: set[str] = set()
    for raw in (new_items or []):
        n = _normalize_item(raw, now)
        if not n:
            continue
        if n["id"] in added_ids:            # P7：同批次重复 → 丢弃，不算再确认
            continue
        cur = by_id.get(n["id"])
        if cur is None:
            n["source_session"] = session_id
            n["first_seen"] = now
            n["last_seen"] = now
            n["confirm_count"] = 1
            by_id[n["id"]] = n
            added_ids.add(n["id"])
        else:
            if n["id"] in old_ids:          # 只有命中旧条目才计入再确认
                cur["confirm_count"] = int(cur.get("confirm_count", 1)) + 1
            cur["last_seen"] = now
            if n.get("evidence"):
                cur["evidence"] = n["evidence"]
            # source_session 只记「首次出现」的来源；再次确认时保持不动（保 provenance 不失真）

    items = list(by_id.values())
    if len(items) > cap:
        items.sort(key=_keep_rank, reverse=True)
        items = items[:cap]
    return items


def _merge(memory: dict, extraction: MemoryExtraction, session_id: str = "", now: str | None = None) -> dict:
    """LLM 输出 → 新记忆。

    preferences 按键 overlay；behavior/observations 条目级合并。
    P4 护栏：本次没有任何新条目、也没有 retract 时，旧列表原样保留（防部分丢失）。
    """
    now = now or datetime.now().isoformat(timespec="seconds")
    new = empty_memory()

    old_prefs = dict(memory.get("preferences") or {})
    new_prefs = {
        k: v for k, v in (extraction.preferences or {}).items()
        if k in PREF_KEYS and v
    }
    new["preferences"] = {**old_prefs, **new_prefs}

    retract = [str(t).strip() for t in (extraction.retract or []) if str(t).strip()]
    retract_ids = {_item_id(t) for t in retract}
    retract_texts = set(retract)

    def _section(old_items, new_items, cap):
        # P4：新列表空且无 retract → 保留旧值不动
        if not new_items and not retract:
            return _normalize_items(old_items, now)
        return _merge_section(
            old_items, new_items, retract_ids, retract_texts, session_id, now, cap
        )

    new["behavior"] = _section(memory.get("behavior"), extraction.behavior, BEHAVIOR_STORE_CAP)
    new["observations"] = _section(
        memory.get("observations"), extraction.observations, OBSERVATIONS_STORE_CAP
    )
    return new


def _has_signal(extraction: MemoryExtraction) -> bool:
    """unchanged=False 但三个通道全空 → 视为无信号(防误清)。"""
    if extraction.unchanged:
        return False
    prefs = {k: v for k, v in (extraction.preferences or {}).items() if k in PREF_KEYS and v}
    return bool(prefs or extraction.behavior or extraction.observations or extraction.retract)


def _diff_stats(old_memory: dict, new_memory: dict) -> dict:
    """本次写入的变更摘要：新增 / 再确认 / 新作废 条数（供 change_log 审计）。"""
    stats = {"added": 0, "confirmed": 0, "retracted": 0}
    for sec in ("behavior", "observations"):
        oldm = {x["id"]: x for x in _normalize_items(old_memory.get(sec))}
        newm = {x["id"]: x for x in _normalize_items(new_memory.get(sec))}
        for i, it in newm.items():
            prev = oldm.get(i)
            if prev is None:
                stats["added"] += 1
            else:
                if int(it.get("confirm_count", 1)) > int(prev.get("confirm_count", 1)):
                    stats["confirmed"] += 1
                if it.get("retracted") and not prev.get("retracted"):
                    stats["retracted"] += 1
    return stats


# ──────────────────────────────────────────────
#  入口:critic 调度的异步抽取
# ──────────────────────────────────────────────


def schedule_extraction(state) -> None:
    """critic_node 在 episode 终结时调用。同步快照 state → 有界后台队列抽取。

    同步快照避免 worker 读 state 的竞争;调度失败/快照失败都静默降级。
    state.user_id 随快照传入（多用户记忆隔离）。
    """
    try:
        msgs = [
            {"role": m.role, "content": m.content}
            for m in (getattr(state, "tutor_messages", None) or [])
        ]
        problem = getattr(state, "problem", None)
        payload = {
            "session_id": getattr(state, "session_id", "") or "",
            "user_id": getattr(state, "user_id", "default") or "default",
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
        from code_tutor_agent.llm_bg_queue import submit_bounded

        # 有界队列而非裸起线程：抽取是大 prompt 的阻塞 LLM 调用，与用户判题共用
        # 同一个 key 的 RPM 桶。连续提交时裸线程没有上界，会把配额吃满让下一个
        # submit 撞 429（叠重试即分钟级等待）。队列满则丢弃——记忆是尽力而为的衍生数据。
        submit_bounded("memory-extract", lambda: _run_extraction(payload))
    except Exception as exc:
        logger.warning("memory: failed to schedule extraction: %s", exc)


def _run_extraction(payload: dict) -> None:
    """后台线程主流程:切片 → 门控 → LLM → merge → 落库。全程 try/except。"""
    try:
        user_id = payload.get("user_id") or "default"
        with _WRITE_LOCK:
            memory = load_memory(user_id)
            messages = payload["messages"]
            session_id = payload["session_id"]

            start = _delta_start(memory, session_id, len(messages))
            delta = messages[start:]
            if not delta:
                return

            user_count = sum(1 for m in delta if m.get("role") == "user")
            if user_count < MIN_USER_MSGS:
                _advance_watermark(memory, session_id, len(messages), user_id)
                logger.info("memory: gate skip (user msgs %d < %d)", user_count, MIN_USER_MSGS)
                return

            transcript = _format_transcript(delta)
            extraction = _call_llm(memory, transcript, payload)

            # P5：失败 ≠ 无信号。失败不推进水位（下次重试），连续超限才放弃该段。
            if extraction is None:
                _on_extract_failed(memory, session_id, len(messages), user_id)
                return

            if not _has_signal(extraction):
                _advance_watermark(memory, session_id, len(messages), user_id)
                logger.info("memory: no stable signal, watermark advanced only")
                return

            now = datetime.now().isoformat(timespec="seconds")
            new_memory = _merge(memory, extraction, session_id=session_id, now=now)
            prev_meta = memory.get("meta", {}) or {}
            revision = int(prev_meta.get("revision", 0) or 0) + 1
            change_log = list(prev_meta.get("change_log") or [])
            change_log.append({"at": now, "revision": revision, **_diff_stats(memory, new_memory)})
            new_memory["meta"] = {
                "updated_at": now,
                "watermark": {"session_id": session_id, "count": len(messages)},
                "fail_count": 0,
                "revision": revision,
                "change_log": change_log[-CHANGE_LOG_CAP:],
            }
            save_memory(new_memory, user_id)
            logger.info(
                "memory: updated (user=%s) — prefs=%d behavior=%d observations=%d",
                user_id,
                len(new_memory["preferences"]), len(new_memory["behavior"]),
                len(new_memory["observations"]),
            )
    except Exception:
        logger.warning("memory: extraction failed", exc_info=True)


def _on_extract_failed(memory: dict, session_id: str, count: int, user_id: str) -> None:
    """LLM 失败：累计 fail_count；不推进水位（下次重试），超限才放弃该段。"""
    meta = memory.setdefault("meta", {})
    fail = int(meta.get("fail_count", 0) or 0) + 1
    if fail >= FAIL_RETRY_LIMIT:
        meta["fail_count"] = 0
        meta["watermark"] = {"session_id": session_id, "count": count}
        logger.warning("memory: extraction failed %d times, give up segment", fail)
    else:
        meta["fail_count"] = fail
        logger.warning(
            "memory: extraction failed (%d/%d), keep watermark for retry", fail, FAIL_RETRY_LIMIT
        )
    save_memory(memory, user_id)


def _advance_watermark(memory: dict, session_id: str, count: int, user_id: str = "default") -> None:
    """无内容更新时只推进水位,避免下次重复处理同一段对话。"""
    memory.setdefault("meta", {})
    memory["meta"]["watermark"] = {"session_id": session_id, "count": count}
    memory["meta"]["fail_count"] = 0
    save_memory(memory, user_id)


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


def _visible_items(items, cap: int) -> list[dict]:
    """过滤 retracted，按最近确认 / 确认次数排序，取前 cap 条。"""
    items = [x for x in _normalize_items(items) if not x.get("retracted")]
    items.sort(key=lambda x: (x.get("last_seen", ""), x.get("confirm_count", 0)), reverse=True)
    return items[:cap]


def render_memory_summary(user_id: str | None = None) -> str:
    """渲染记忆为紧凑文本块,供 agent_dialog 注入;空记忆 → 空串。"""
    try:
        memory = load_memory(user_id)
        prefs = memory.get("preferences") or {}
        behavior = _visible_items(memory.get("behavior"), BEHAVIOR_CAP)
        observations = _visible_items(memory.get("observations"), OBSERVATIONS_CAP)
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
            parts.extend(f"- {b['text']}" for b in behavior)
        if observations:
            parts.append("观察:")
            parts.extend(f"- {o['text']}" for o in observations)
        return "\n".join(parts)
    except Exception as exc:
        logger.warning("memory: render failed (ignored): %s", exc)
        return ""
