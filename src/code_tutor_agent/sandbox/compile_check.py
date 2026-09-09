"""编译错误预检 + ``^`` 指针构造（2026-09-09，LeetCode 风格 CE 指示器）。

核心设计（实证见 .workbuddy/memory/2026-09-09.md）：
- 在构建 harness **之前**对用户代码**单独** ``compile()``——语法错时行号天然是
  用户编辑器行号（无需 harness 行号 remap），且不会泄露 harness 内部代码
  （未闭合三引号把 harness 吞进字符串的 badcase：harness 级报错行号落在
  harness 内、``.text`` 泄露 ``print('RESULT: ...')`` 等内部代码）。
- 指针公式：``" " * (col-1) + "^" * max(1, end-col+1)``，与 CPython/LeetCode 一致。

安全（2026-09-09 安全评审终版）：
- ``compile()`` 只解析不执行（``exec()`` 才执行），预校验本身无 RCE/副作用；
- 解析炸弹由 API 入口的 ``enforce_code_limits``（10KB / 300 行，reject 非
  truncate）拒收，本函数不重复检查、不做截断（截断后行号无意义）；
- 深嵌套导致的 ``RecursionError`` 捕获降级为普通 CE，不让 500 打穿判题链路。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 与 AC / WA / RE / TLE 并列的执行引擎状态字面值（verdict 归约见 agents/agent_judge.py）
COMPILE_ERROR_STATUS = "Compile Error"
VERDICT_CE = "CE"

_TAB_WIDTH = 4


def _display_col(raw_line: str, raw_col: int) -> int:
    """把 raw 列号（1 起）换算到 Tab 展开（expandtabs(4)）后的展示列号。"""
    if raw_col is None or raw_col < 1:
        return 1
    return len(raw_line[: raw_col - 1].expandtabs(_TAB_WIDTH)) + 1


def build_compile_error(exc: BaseException) -> dict:
    """由 SyntaxError（或同形异常）构造 CompileErrorInfo payload。

    形状（Phase 0 契约）::

        {status, line, column, text, pointer, message, human}

    - ``text``/``pointer`` 均为 Tab 展开后的展示口径，配 ``<pre>`` 渲染不错位；
    - ``human`` 为多行降级渲染串（纯文本场景 / LLM 判题分析用）。
    """
    line = getattr(exc, "lineno", None) or 1
    col = getattr(exc, "offset", None) or 1
    end_col = getattr(exc, "end_offset", None) or (col + 1)
    if end_col < col:
        end_col = col
    msg = getattr(exc, "msg", None) or "invalid syntax"
    raw_text = getattr(exc, "text", None) or ""
    raw_line = raw_text.split("\n")[0]

    disp_line = raw_line.expandtabs(_TAB_WIDTH).rstrip()
    disp_col = _display_col(raw_line, col)
    disp_end = _display_col(raw_line, end_col)
    width = max(1, disp_end - disp_col)
    pointer = (" " * (disp_col - 1) + "^" * width) if disp_line else ""

    prefix = f"Line {line}: "
    human = f"{prefix}{msg}"
    if disp_line:
        human += f"\n{prefix}{disp_line}\n{' ' * len(prefix)}{pointer}"

    return {
        "status": COMPILE_ERROR_STATUS,
        "line": line,
        "column": disp_col,
        "text": disp_line,
        "pointer": pointer,
        "message": msg,
        "human": human,
    }


def check_compile(code: str, filename: str = "Solution.py") -> dict | None:
    """编译预检：语法错返回 :func:`build_compile_error` 的 payload，通过返回 ``None``。

    用户代码单独编译（不进 harness），因此报错行号 = 编辑器行号。
    ``compile()`` 只解析不执行，可安全地在进程内对不可信代码调用。
    """
    try:
        compile(code, filename, "exec")
        return None
    except SyntaxError as exc:
        info = build_compile_error(exc)
        logger.info("compile pre-check failed: line %s col %s — %s", info["line"], info["column"], info["message"])
        return info
    except (RecursionError, MemoryError) as exc:
        # 深嵌套解析炸弹：限长闸门（10KB）之后仍可能的紧凑炸弹，降级为普通 CE，
        # 绝不让 500 打穿判题链路。
        logger.warning("compile pre-check resource error: %s", type(exc).__name__)
        return {
            "status": COMPILE_ERROR_STATUS,
            "line": 0,
            "column": 0,
            "text": "",
            "pointer": "",
            "message": "代码嵌套过深或体积异常，无法解析，请精简后重试",
            "human": "Compile Error: 代码嵌套过深或体积异常，无法解析，请精简后重试",
        }
    except ValueError as exc:
        # 典型：源码含 null bytes（compile 拒绝）
        logger.info("compile pre-check ValueError: %s", exc)
        return {
            "status": COMPILE_ERROR_STATUS,
            "line": 0,
            "column": 0,
            "text": "",
            "pointer": "",
            "message": f"代码无法编译：{exc}",
            "human": f"Compile Error: 代码无法编译：{exc}",
        }
