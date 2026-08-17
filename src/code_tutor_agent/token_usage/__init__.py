"""Token 计量与成本控制模块。

组成:
- cost:       单价配置 + 成本折算 + 缓存命中率
- sink:       异步批量落库(内存队列 + 后台线程)
- callback:   零侵入 LLM 用量回调(BaseCallbackHandler)
"""
from code_tutor_agent.token_usage.sink import get_token_sink, token_sink

__all__ = ["get_token_sink", "token_sink"]
