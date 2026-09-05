"""设计类题目围栏（design guard）。

系统判题引擎只支持「单方法函数题」：主实现类必须是 ``class Solution``，
传入输入、一次调用返回结果即完成判定。设计类题目（LRU 缓存、最小栈、
前缀树等）的模板主类是 ``class LRUCache`` / ``class MinStack`` 等非
Solution 类，需要「实例化 + 按操作序列回放」的判题驱动器，暂不支持。

本模块提供统一的检测函数与拒绝话术，三处入口共用（双重防护里的"硬规则"层）：
- LeetCode 导入：``leetcode_fetcher.fetch_problem`` 抓到设计题直接报错；
- LLM 出题硬校验：``agent_problem.verify_problem`` / ``generation.verifier``
  对生成结果做结构级拒绝（即使 prompt 约束被无视也出不了设计题）；
- 对话引导：``agent_dialog`` 的 prompt 约束 + 意图硬守护（关键词兜底）。
"""

from __future__ import annotations

import re

# 用户可见的拒绝说明（拼在错误/提示消息里）
DESIGN_REJECT_SUFFIX = (
    "暂不支持设计类题目：判题引擎目前只支持单方法函数题（class Solution 模板、"
    "一次调用返回结果即完成判定），设计类题目（如 LRU 缓存、LFU 缓存、最小栈、"
    "前缀树等需要实现自定义类并按操作序列多次调用的题）暂无法判题。"
)

# 对话侧话题关键词兜底（prompt 约束被无视时的硬守护）。
# 保守取词：只收录明确指向「实现自定义类/操作序列」的表达，避免误伤
# 单调队列、双端队列等在普通函数题里也常见的话题词。
_DESIGN_TOPIC_KEYWORDS = (
    "lru", "lfu", "least recently used",
    "前缀树", "trie",
    "最小栈", "最大栈",
    "用栈实现队列", "用队列实现栈", "实现栈", "实现队列",
    "设计哈希", "设计推特", "设计类", "设计题", "数据结构设计",
    "设计一道", "设计一个类",
)


def _strip_fences_and_comments(code: str) -> str:
    """剥掉 markdown 围栏和注释行，避免注释里的 class 字样干扰判定。"""
    text = code or ""
    # 去掉 ```python ... ``` 围栏（取围栏内内容）
    if "```" in text:
        blocks = re.findall(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
        text = "\n".join(blocks) if blocks else text
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # 去行尾注释（class 判定不依赖字符串字面量精度，简单切分即可）
        line = re.sub(r"#.*$", "", line)
        kept.append(line)
    return "\n".join(kept)


def extract_main_classes(code: str) -> list[str]:
    """提取代码里所有顶层 class 名（忽略缩进的嵌套类）。"""
    cleaned = _strip_fences_and_comments(code)
    return re.findall(r"^class\s+(\w+)", cleaned, re.MULTILINE)


def is_design_style_code(code: str) -> bool:
    """判定代码模板是否是「设计类」风格 —— 主实现类不是 Solution。

    判定口径（题目模板必须是 Solution 开头）：
    - 顶层没有任何 class → 不判为设计类（交给下游校验，如空 starter）；
    - 顶层类里存在 Solution → 功能题（TreeNode/ListNode 等辅助类不算）；
    - 顶层类全部不是 Solution（如 LRUCache / MinStack / MyHashSet）→ 设计类。
    """
    classes = extract_main_classes(code)
    if not classes:
        return False
    return not any(name == "Solution" for name in classes)


def mentions_design_topic(text: str) -> bool:
    """对话文本/话题是否命中设计类题目关键词（大小写不敏感）。"""
    if not text:
        return False
    lowered = text.lower()
    return any(kw in lowered for kw in _DESIGN_TOPIC_KEYWORDS)


def design_dialog_refusal(topic_hint: str = "") -> str:
    """对话侧的拒答+引导话术。"""
    alt = ""
    if topic_hint:
        alt = f"我们可以换个思路：把「{topic_hint}」练成它的应用型单方法题"
    return (
        "这个方向我得先打个招呼：" + DESIGN_REJECT_SUFFIX
        + (alt + "，比如哈希查找、统计、栈的合法性判定这类一次调用就能判分的题。"
           "要不要从这些方向里挑一个？" if alt else
           "不如换一个方向？比如哈希查找、统计、栈的合法性判定这类一次调用就能判分的题。")
    )
