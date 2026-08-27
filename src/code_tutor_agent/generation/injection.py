"""出题注入模块（方案 H：F/G 随机二选一，不混用）。

背景与定位
----------
防碰撞实验（见 ``方案选择过程.md``）证明：
- 方案 F（随机场景注入）：把知识点套不同生活/业务背景（外卖/地铁/社交…），
  多样性在**外层包装**，碰撞率近零，但偶发写出废题（成功率 60~90%）。
- 方案 G（维度文件驱动）：从手写维度 JSON 随机抽 1 个算法角度（空间压缩/树形DP…）
  注入，多样性在**内层算法角度**，题更贴知识点、更「教学向」，成功率更高，
  但会坍缩到维度招牌题（碰撞 3~9%）。
- 方案 G+F 双随机（同 prompt 叠加）实测**双输**：双约束压垮模型输出，成功率暴跌。

结论（用户 2026-08-27 最终决策＝方案 H）：每次出题按概率**随机选 F 或 G 一种**，
两种注入内容**绝不混在同一 prompt**（「或」而非「与」），取 F 低碰撞 + G 高成功率之长。

本模块职责
----------
- 提供 ``decide_injection(topic, rng)``：抛硬币选 F/G，返回 ``(mode, suffix)``；
  G 模式无维度文件时自动 fallback 到 F（保证每次都能出题）。
- 提供 F 的 20 条场景池、G 的维度文件读取与拼装逻辑（均从验证过的探针搬迁）。
- suffix 仅追加到 USER prompt 末尾，且对花括号转义（LangChain 模板安全）。

配置项（读取自环境变量，详见 config 模块）：
- ``H_ENABLED``：是否启用方案 H（默认 True）。
- ``H_PROBABILITY_F``：选 F 的概率（默认 0.5）。
- ``H_DIM_DIR``：维度文件目录（默认 data/problem-dimension）。
"""
from __future__ import annotations

import json
import os
import random
from typing import Optional

# ── 配置（从环境变量读取，缺省给安全默认值） ──
H_ENABLED = os.getenv("H_ENABLED", "1") == "1"
H_PROBABILITY_F = float(os.getenv("H_PROBABILITY_F", "0.5"))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
H_DIM_DIR = os.getenv(
    "H_DIM_DIR",
    os.path.join(_ROOT, "data", "problem-dimension"),
)

# ── 方案 F：随机场景池（外层包装，逼模型重组「场景×内核」） ──
# 每条是一个完整的背景设定句，注入时作为「场景灵感」提示。
SCENARIOS = [
    "背景设定在一个外卖配送调度系统里",
    "背景设定在一个地铁/公交实时换乘导航产品里",
    "背景设定在一个社交平台的动态推送与好友推荐系统里",
    "背景设定在一个电商大促的优惠券与满减计算系统里",
    "背景设定在一个在线音乐/歌单编辑与智能推荐产品里",
    "背景设定在一个停车场出入管理与车位调度系统里",
    "背景设定在一个图书馆/档案馆的图书编目与检索系统里",
    "背景设定在一个健身房课程排期与会员预约系统里",
    "背景设定在一个物流仓储的货位管理与拣货路径规划里",
    "背景设定在一个股票/基金行情看板与量化回测工具里",
    "背景设定在一个短视频平台的投稿审核与流量分发系统里",
    "背景设定在一个医院叫号与诊室资源调度系统里",
    "背景设定在一个在线教育平台的作业批改与学习路径推荐里",
    "背景设定在一个智能家居设备的联动场景编排里",
    "背景设定在一个电子游戏的关卡掉落与资源产出系统里",
    "背景设定在一个城市交通信号灯的配时优化系统里",
    "背景设定在一个票务系统的座位分配与改签流程里",
    "背景设定在一个云服务的自动扩缩容与任务调度里",
    "背景设定在一个代码评审/CI 流水线的变更影响分析里",
    "背景设定在一个天气/航线预测的实时数据处理管道里",
]

# ── 方案 G：维度文件读取 ──
def _resolve_dim_file(topic: str) -> Optional[str]:
    """按 topic 精确匹配维度文件；找不到返回 None。

    维度文件名即 topic（如 ``数组.json``）。不做模糊匹配，
    避免把不相关 topic 的维度误注入。
    """
    fp = os.path.join(H_DIM_DIR, f"{topic}.json")
    return fp if os.path.exists(fp) else None


def load_dimensions(topic: str) -> Optional[list[dict]]:
    """读取某 topic 的维度列表；无维度文件或解析失败返回 None。"""
    fp = _resolve_dim_file(topic)
    if not fp:
        return None
    try:
        data = json.load(open(fp, encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, list) or not data:
        return None
    return data


def _escape_braces(s: str) -> str:
    """转义花括号，避免被 LangChain ChatPromptTemplate 当成变量。

    仅转义 suffix 文本；原 ``GENERATE_PROBLEM_USER`` 自带的 ``{topic}`` /
    ``{difficulty}`` 占位符由 agent_problem 负责，不经过此函数。
    """
    return s.replace("{", "{{").replace("}", "}}")


def build_f_suffix(rng: random.Random, topic: str) -> str:
    """构造方案 F 的场景注入段。"""
    scenario = rng.choice(SCENARIOS)
    # 注意：suffix 内的知识点名用 topic 实参直接代入（不依赖 LangChain 占位符），
    # 避免 _escape_braces 把 {topic} 误转义成字面量。
    return (
        "\n\n"
        "## 场景灵感（仅作包装启发，不必在题干里原样出现）\n"
        "本次请以如下随机场景为灵感来设计题目背景，使同类知识点下的题目背景明显多样化：\n"
        f"背景设定在一个{scenario.lstrip('背景设定在')}\n"
        f"注意：算法内核仍需紧扣知识点 {topic}，但场景包装、输入输出故事应与此灵感呼应，"
        "不要每次都出最经典的 LeetCode 原题变体。"
    )


def build_g_suffix(rng: random.Random, topic: str) -> Optional[str]:
    """构造方案 G 的维度注入段；无维度文件返回 None（调用方应 fallback 到 F）。"""
    dims = load_dimensions(topic)
    if not dims:
        return None
    dim = rng.choice(dims)
    name = dim.get("dimension_name", "")
    desc = dim.get("description", "")
    ex = dim.get("example_directions", [])
    if isinstance(ex, list) and ex:
        ex_lines = "\n".join(f"    · {e}" for e in ex)
        ex_block = f"  可参考的具体方向：\n{ex_lines}\n"
    else:
        ex_block = ""
    return (
        "\n\n"
        "## 本次出题参考维度（从该题型的精选维度中随机选取，请据此设计题目；"
        f"算法内核仍须紧扣知识点 {topic}）\n"
        f"- 维度：{name}\n"
        f"  思路：{desc}\n"
        f"{ex_block}"
    )


def decide_injection(
    topic: str,
    rng: Optional[random.Random] = None,
) -> tuple[str, str]:
    """方案 H 核心：随机二选一，返回 (mode, suffix)。

    - mode: ``"F"``（场景注入）或 ``"G"``（维度注入）。
    - suffix: 追加到 USER prompt 末尾的注入文本（已转义花括号）。
    - 若选 G 但该 topic 无维度文件，自动降级为 F（保证可出题）。

    ``H_ENABLED=False`` 时直接返回 ``("", "")``（不注入，纯基线出题）。
    """
    if not H_ENABLED:
        return "", ""
    rng = rng or random.Random()
    use_f = rng.random() < H_PROBABILITY_F
    if use_f:
        return "F", _escape_braces(build_f_suffix(rng, topic))
    # 选 G：无维度文件则 fallback F
    g = build_g_suffix(rng, topic)
    if g is None:
        return "F", _escape_braces(build_f_suffix(rng, topic))
    return "G", _escape_braces(g)
