"""结构化 JSON 日志配置。

每条日志输出为一行 JSON，包含 request_id 贯穿整个请求链路。
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta
from typing import Any


# ── 请求链路追踪：每个请求的唯一 ID ──
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    """将 LogRecord 格式化为单行 JSON。

    自动包含 request_id（从 ContextVar 读取），
    并透传所有通过 logging extra= 传入的自定义字段。
    """

    # LogRecord 内置属性名，避免冲突
    _RESERVED = frozenset({
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName",
        "taskName",  # Python 3.12+
    })

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            # 使用北京时间（UTC+8）输出日志时间戳，避免与本地时间相差 8 小时
            "timestamp": datetime.now(timezone(timedelta(hours=8))).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_ctx.get("-"),
        }
        # 透传 extra= 传入的自定义字段
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                log_entry[key] = value
        # 异常信息
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = str(record.exc_info[1])
        return json.dumps(log_entry, ensure_ascii=False, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    """安装结构化 JSON 日志处理器，替换所有现有 handler。

    调用时机：应用启动时，在任何 logger 使用之前。
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(handler)

    # 降噪第三方库的 DEBUG/INFO 日志
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_request_id() -> str:
    """获取当前请求的 request_id，未在请求上下文中时返回 '-'。"""
    return request_id_ctx.get("-")


def generate_request_id() -> str:
    """生成一个短的 request_id，用于 Header 或日志。"""
    return str(uuid.uuid4())[:12]
