"""用户代码输入闸门（10KB / 300 行，2026-09-09）。

职责：
1. run / submit 入口硬校验：超限直接 413 拒收，不进判题链路（不烧配额、
   不占并发信号量、不写 submissions）；
2. 后续「编译错误指针」预检（check_compile）复用本函数作为第一道闸门；
3. 与前端编辑器 300 行硬拦守同一条线：前端拦截 → 后端 413 → 存储层封顶。

设计依据（2026-09-09 安全评审）：
- 限长是 **reject 而非 truncate**：截断后编译的报错行号指向被砍代码、无意义，
  且紧凑嵌套的解析炸弹（几 KB）截断也挡不住；
- 超限错误信息直接面向用户展示（前端 413 时解析 detail 呈现在对话流里）。
"""
from __future__ import annotations

from fastapi import HTTPException

from code_tutor_agent.config import MAX_CODE_BYTES, MAX_CODE_LINES


class CodeTooLargeError(ValueError):
    """提交代码超出大小/行数上限（message 为用户可读文案）。"""


def check_code_limits(code: str) -> None:
    """校验用户代码大小与行数，超限抛 :class:`CodeTooLargeError`（不修改代码）。

    行数口径与前端一致：``split('\\n').length``（即 ``count("\\n") + 1``），
    保证「前端 300 行硬拦」与「后端 300 行硬拒」是同一条线。
    """
    n_bytes = len(code.encode("utf-8", errors="replace"))
    if n_bytes > MAX_CODE_BYTES:
        raise CodeTooLargeError(
            f"代码过大：{n_bytes} 字节，超过上限 {MAX_CODE_BYTES} 字节"
            f"（约 {MAX_CODE_BYTES // 1024}KB），请精简后提交"
        )
    n_lines = code.count("\n") + 1
    if n_lines > MAX_CODE_LINES:
        raise CodeTooLargeError(
            f"代码过长：{n_lines} 行，超过上限 {MAX_CODE_LINES} 行，请精简后提交"
        )


def enforce_code_limits(code: str) -> None:
    """路由层便捷入口：超限抛 ``HTTPException(413, 用户可读文案)``。"""
    try:
        check_code_limits(code)
    except CodeTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc))
