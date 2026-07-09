"""每个会话的生成进度存储。

线程安全的共享字典，graph 节点和 API 端点同时读写。
"""

# sid → list of progress message strings
_generation_progress: dict[str, list[str]] = {}