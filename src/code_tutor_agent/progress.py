"""每个会话的生成进度存储。

线程安全的共享字典，graph 节点和 API 端点同时读写。
"""

# sid → list of progress message strings
_generation_progress: dict[str, list[str]] = {}

# sid → 出题命中通道（llm / leetcode_import / leetcode_pull / db_unac / static）
# 由 generator_node 写入（generation 包产出 GenerationResult.channel），
# serializer 透出给前端，UI 能说清题目来源（docs/generation-subagent-design.md §7）。
_generation_channels: dict[str, str] = {}


def record_generation_channel(sid: str, channel: str | None) -> None:
    """记录会话的出题通道（None 不覆盖已有值）。"""
    if channel:
        _generation_channels[sid] = channel


def get_generation_channel(sid: str) -> str:
    """读取会话的出题通道，未记录返回空串。"""
    return _generation_channels.get(sid, "")
