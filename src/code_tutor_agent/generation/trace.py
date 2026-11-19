"""出题修复 trace：每次出题追加一条 JSONL。

只做旁路记录，任何异常都吞掉，绝不影响出题主流程。
落点默认 ``logs/gen_repair.jsonl``，可用 ``CTA_GEN_TRACE_PATH`` 覆盖；
设置为空字符串可关闭。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()


def trace_enabled() -> bool:
    return bool(os.getenv("CTA_GEN_TRACE_PATH", "logs/gen_repair.jsonl").strip())


def record_gen_trace(record: dict) -> None:
    if not trace_enabled():
        return
    path = Path(os.getenv("CTA_GEN_TRACE_PATH", "logs/gen_repair.jsonl"))
    payload = {"ts": datetime.now().isoformat(timespec="milliseconds"), **record}
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.debug("gen trace write failed (ignored): %s", exc)
