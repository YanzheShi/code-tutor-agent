"""主题目录 — 前端「出题选择器」与后端生成链路的唯一真源。

- 值即中文主题名：前端把主题拼进自然语言消息（`请出一道…主题关于「XX」的算法题`），
  agent_dialog 的正则兜底（known_topics）、generator 的 ``_TOPIC_TAG_MAP`` / LLM 网关的
  ``_TOPIC_GEN_MAP`` 都以中文名为键，因此 value 必须保持中文，不能改成英文 slug。
- 列表为「精选集」：只收 UI 可展示、且生成链路已覆盖的主题；别名（图论/图遍历/树的
  dfs/优先队列…）不进目录，避免 UI 按钮噪音。
- 前端加载失败时回退内置静态列表，故新增主题只需改这里 + 前端兜底列表两处。
"""

from __future__ import annotations

# name 顺序即前端按钮展示顺序（随机主题由前端自行追加，不进目录）
TOPIC_CATALOG: list[str] = [
    "数组",
    "双指针",
    "滑动窗口",
    "二分查找",
    "链表",
    "栈",
    "队列",
    "哈希表",
    "动态规划",
    "字符串",
    "递归",
    "回溯",
    "贪心",
    "位运算",
    "排序",
    "前缀和",
    "图",
    "拓扑排序",
    "并查集",
    "二叉树",
    "堆",
    "数论",
]

TOPICS_RESPONSE: dict = {"topics": [{"value": t, "label": t} for t in TOPIC_CATALOG]}