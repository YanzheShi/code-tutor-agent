"""Token 成本计算与缓存命中率。

集中管理单价(随网关调价只改这里)、成本折算、缓存命中率口径、
以及 purpose → 业务分类的映射。

单价口径(单位:元 / 千 token),与 DeepSeek 官方一致:
- input / output 普通单价
- cache_write = 写入缓存倍率(DeepSeek 1.25×)
- cache_read  = 缓存命中折扣(DeepSeek 0.1×)
"""
from __future__ import annotations

# ── 单价配置(元 / 千 token) ──
# 默认档:覆盖未知模型。DeepSeek 系按名称子串匹配。
PRICING: dict[str, dict[str, float]] = {
    "deepseek-chat": {
        "input": 1.0,
        "output": 2.0,
        "cache_write": 1.25,   # 写入缓存倍率
        "cache_read": 0.1,     # 命中折扣
    },
    "deepseek-reasoner": {
        "input": 4.0,
        "output": 16.0,
        "cache_write": 1.25,
        "cache_read": 0.1,
    },
}
DEFAULT_PRICING: dict[str, float] = {
    "input": 1.0,
    "output": 2.0,
    "cache_write": 1.25,
    "cache_read": 0.1,
}

# ── purpose → 业务分类(用于前端分组与诊断) ──
PURPOSE_CATEGORY: dict[str, str] = {
    "problem": "出题",
    "generator": "出题",
    "api-generation": "出题",
    "api-generation-high": "出题",
    "judge": "判题",
    "adversarial-eval": "判题",
    "adversarial-eval-low": "判题",
    "tutor-eval": "辅导",
    "tutor-generate": "辅导",
    "tutor-router": "辅导",
    "tutor": "辅导",
    "dialog": "对话",
    "dialog-stream": "对话",
    "chat": "对话",
    "api-chat": "对话",
    "api-chat-query": "对话",
    "memory-extract": "记忆",
    "context-summary": "上下文",
    "edit-trace": "轨迹",
    "benchmark": "基准",
}


def category_of(purpose: str) -> str:
    """返回 purpose 的业务分类,未知归为「其他」。"""
    return PURPOSE_CATEGORY.get(purpose, "其他")


def pick_pricing(model_name: str) -> dict[str, float]:
    """按模型名选择单价表;名称含已知厂商子串则命中,否则用默认档。"""
    if not model_name:
        return DEFAULT_PRICING
    low = model_name.lower()
    for key, table in PRICING.items():
        if key in low:
            return table
    return DEFAULT_PRICING


def cost_calculator(model_name: str, rec: dict) -> float:
    """按 input / output / cache_write / cache_read 四类单价折算成本(元)。

    ``prompt_tokens`` 为原始输入总量;其中 ``cache_creation``(写入新缓存)
    与 ``cache_read``(命中)之外的部分按普通 input 计价。
    """
    p = pick_pricing(model_name)
    non_cached_input = (
        rec["prompt_tokens"] - rec["cache_creation_tokens"] - rec["cache_read_tokens"]
    )
    if non_cached_input < 0:
        non_cached_input = 0
    cost = (
        non_cached_input * p["input"]
        + rec["completion_tokens"] * p["output"]
        + rec["cache_creation_tokens"] * p["cache_write"]
        + rec["cache_read_tokens"] * p["cache_read"]
    ) / 1000.0
    return round(cost, 6)


def cache_hit_rate(rec: dict) -> float:
    """缓存命中率(0~1)。

    口径:命中只统计 ``cache_read``;``cache_creation`` 是写入新缓存,不计入命中。
    命中率 = cache_read / (cache_read + 非缓存输入)。

    兼容两种键名:明细行(聚合查询构造的 rec)用 ``*_tokens`` 后缀;
    而 ``_period_totals`` 返回的是简写(``cache_creation`` / ``cache_read`` /
    ``prompt``),这里做兜底读取,避免 KeyError。
    """
    read = rec.get("cache_read_tokens", rec.get("cache_read", 0) or 0)
    creation = rec.get("cache_creation_tokens", rec.get("cache_creation", 0) or 0)
    prompt = rec.get("prompt_tokens", rec.get("prompt", 0) or 0)
    non_cached = prompt - creation - read
    base = read + non_cached
    return (read / base) if base > 0 else 0.0
