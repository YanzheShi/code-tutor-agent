"""错误模式维度画像：6 维枚举 + 增量输出 schema + 纯函数聚合。

与 v2 的「知识点画像」(prof per tag) 正交，记录「错误模式」(跨知识点的通病)：
边界越界 / 浅拷贝 / 死循环 / map 漏检 等。详见 docs/error-mode-tracking-design.md。

本模块是纯逻辑，无 IO、无 LLM 依赖，便于单测。
"""
from __future__ import annotations

import time

from pydantic import BaseModel, Field

# ── 6 维定义 ──
DIM_KEYS = ["correctness", "datastruct", "perf", "algo", "impl", "debug"]

DIM_DISPLAY = {
    "correctness": "正确性 & 边界",
    "datastruct": "数据结构操作",
    "perf": "复杂度 & 性能",
    "algo": "算法思维",
    "impl": "实现质量与鲁棒性",
    "debug": "自测与调试",
}

# 各维度合法小项（slug），全固化。LLM function_calling 输出必须命中其中之一。
WEAKNESS_TAGS: dict[str, list[str]] = {
    "correctness": ["boundary", "index_oob", "none_handling", "div_zero", "float_prec"],
    "datastruct": ["shallow_vs_deep", "mutable_default", "unhashable_key", "linkedlist_ptr"],
    "perf": ["tle_brute", "no_memo", "greedy_instead_dp", "recursion_depth"],
    "algo": ["wrong_struct", "suboptimal", "binary_loop", "dp_state"],
    "impl": ["god_func", "dup_code", "bad_naming", "global_leak"],
    "debug": ["no_self_test", "blind_fix", "traj_stuck", "traj_churn"],
}

VALID_TAGS = {tag for tags in WEAKNESS_TAGS.values() for tag in tags}

# ── 聚合参数（见文档 §7）──
DECAY = 0.85                  # 命中即衰减系数
SEVERITY_OLD_W = 0.6         # severity 历史权重
SEVERITY_NEW_W = 0.4         # severity 本次权重
VERDICT_BOOST = 1.3          # 判题失败补充 feeder 的加权倍数


def is_valid_tag(dim: str, tag: str) -> bool:
    """防御：LLM 可能吐出未知 slug，必须丢弃。"""
    return dim in WEAKNESS_TAGS and tag in WEAKNESS_TAGS.get(dim, [])


def empty_error_modes() -> dict:
    return {dim: {} for dim in DIM_KEYS}


# ── LLM 增量输出 schema（function_calling）──
class ErrorModeDelta(BaseModel):
    """单次编辑轨迹分析产出的一个错误模式增量。"""
    dim: str = Field(description="6 维之一：correctness/datastruct/perf/algo/impl/debug")
    tag: str = Field(description="该维度的具体错误模式 slug（必须命中 WEAKNESS_TAGS）")
    delta_count: int = Field(default=1, ge=0, description="本次该模式暴露次数")
    severity: float = Field(default=0.5, ge=0.0, le=1.0, description="本次严重度 0~1")
    evidence: str = Field(default="", description="来自轨迹的简要证据（供展示/调试）")


class EditTraceAnalysis(BaseModel):
    """LLM 对一次编辑轨迹的分析结果（增量，非绝对画像）。"""
    deltas: list[ErrorModeDelta] = Field(default_factory=list)


# ── 聚合（纯函数，不修改入参）──
def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def boost_verdict_deltas(
    deltas: list[ErrorModeDelta], multiplier: float = VERDICT_BOOST
) -> list[ErrorModeDelta]:
    """判题失败补充 feeder：对传入的 (dim, tag) delta 整体 ×multiplier（仅上调 count/severity）。

    编辑轨迹分析的 deltas 不 boost（基准）；判题失败提取的 deltas 先 boost 再合并。
    """
    out: list[ErrorModeDelta] = []
    for d in deltas:
        out.append(ErrorModeDelta(
            dim=d.dim,
            tag=d.tag,
            delta_count=int(round(d.delta_count * multiplier)),
            severity=min(1.0, d.severity * multiplier),
            evidence=d.evidence,
        ))
    return out


def apply_deltas(old: dict, deltas: list[ErrorModeDelta]) -> dict:
    """把增量合并进旧 error_modes（时间衰减 + 叠加 + 封顶）。

    每次调用代表一次练习/分析时间步：
    - 先对所有已有 tag 施加时间衰减（久不犯则自然淡出）：
        count   ← count * DECAY
        severity ← severity * SEVERITY_OLD_W   （无新证据时的衰减系数）
    - 再叠加本次 deltas（同一 tag 的多次暴露累加）：
        count   ← decayed_count + Σdelta_count
        severity ← min(1, decayed_severity + Σ(delta.severity * SEVERITY_NEW_W))
      对单 delta 的常见情形，等价于设计公式 old*DECAY + delta。
    - 防御:     未知 (dim, tag) 的 delta 直接丢弃；old 视为可信原样拷贝后衰减。
    """
    result: dict[str, dict[str, dict]] = {dim: {} for dim in DIM_KEYS}
    now = _now_iso()

    # 1) 时间衰减：拷贝所有旧 tag 并衰减
    for dim in DIM_KEYS:
        for tag, prev in (old.get(dim, {}) or {}).items():
            result[dim][tag] = {
                "count": round(prev.get("count", 0.0) * DECAY, 3),
                "severity": round(prev.get("severity", 0.0) * SEVERITY_OLD_W, 3),
                "last_seen": prev.get("last_seen", ""),
                "evidence": prev.get("evidence", ""),
            }

    # 2) 叠加新 delta（防御：未知 (dim,tag) 丢弃）
    for d in deltas:
        if not is_valid_tag(d.dim, d.tag):
            continue
        prev = result[d.dim].get(
            d.tag, {"count": 0.0, "severity": 0.0, "last_seen": "", "evidence": ""}
        )
        new_count = prev["count"] + max(0, d.delta_count)
        new_sev = min(1.0, prev["severity"] + d.severity * SEVERITY_NEW_W)
        result[d.dim][d.tag] = {
            "count": round(new_count, 3),
            "severity": round(new_sev, 3),
            "last_seen": now,
            "evidence": d.evidence or prev.get("evidence", ""),
        }

    # 注：不额外封顶。全固化枚举下每个 dim 合法 slug 上限为 5（correctness），
    # 结构上限已天然约束，无需运行期淘汰分支（原 CAP_PER_DIM=6 为不可达死代码，已删）。
    return result
