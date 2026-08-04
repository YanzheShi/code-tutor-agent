"""沙箱执行器 — 对测试用例执行参考解。

D2：被生成器的自验证循环使用，用于验证
optimal_solution（必须通过）和参考解。

兼容 Windows：使用 subprocess + timeout 替代 resource.RLIMIT。
"""

from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
import tempfile
from typing import Any

logger = logging.getLogger(__name__)

import re as _re
_RE_TEMP = _re.compile(r'File "[^"]+[\\/]tmp[^"]+\.py"')


def _clean_error(stderr: str) -> str:
    """Strip temp-file paths from a traceback and return the last error line."""
    if not stderr:
        return ""
    cleaned = _RE_TEMP.sub("line", stderr)
    lines = cleaned.strip().split("\n")
    for line in reversed(lines):
        line = line.strip()
        if line and not line.startswith(("Traceback", "File ", "  ", "^", "~")):
            return line[:200]
    return cleaned[-200:]


# ── Tunables ──
TIMEOUT_SECONDS = 10.0      # how long before TLE (per test-case suite)
HARNESS_TIMEOUT = 2.0       # outer subprocess timeout (includes startup + TLE guard)

# 跑不可信用户代码的子进程只允许携带这些环境变量。
# 绝不能透传整个 os.environ —— 里面有 LLM_API_KEY 等敏感配置，
# 用户代码 `import os; print(os.environ)` 就能拖走。
_SUBPROC_ENV_WHITELIST = ("PATH", "SystemRoot", "SystemDrive", "HOME", "TMPDIR", "TEMP", "TMP")


def _build_subprocess_env() -> dict:
    """构造子进程的最小环境变量（白名单 + 强制 utf-8 IO）。"""
    env = {k: os.environ[k] for k in _SUBPROC_ENV_WHITELIST if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class RunnerResult:
    """Result of running one solution against one test case."""

    def __init__(
        self,
        test_case_id: int,
        status: str,
        detail: str = "",
        runtime_ms: float = 0.0,
        memory_kb: float = 0.0,
        input_args: list[str] | None = None,
        expected_output: str = "",
        actual_output: str = "",
    ):
        self.test_case_id = test_case_id
        self.status = status       # Passed / Wrong Answer / Runtime Error / TLE
        self.detail = detail
        self.runtime_ms = runtime_ms
        self.memory_kb = memory_kb
        self.input_args = input_args or []
        self.expected_output = expected_output
        self.actual_output = actual_output

    def to_dict(self) -> dict:
        return {
            "test_case_id": self.test_case_id,
            "status": self.status,
            "detail": self.detail,
            "runtime_ms": self.runtime_ms,
            "memory_kb": self.memory_kb,
            "input_args": self.input_args,
            "expected_output": self.expected_output,
            "actual_output": self.actual_output,
        }


def _extract_python_code(text: str) -> str:
    """Extract Python code from LLM response, stripping markdown fences."""
    if match := __import__("re").search(r"```(?:python)?\s*\n(.*?)```", text, __import__("re").DOTALL):
        return match.group(1).strip()
    return text.strip()


def _has_class_solution(text: str) -> bool:
    return "class Solution" in text


def run_solution(
    code: str,
    test_cases: list[dict],
    timeout: float = TIMEOUT_SECONDS,
    function_signature: str | None = None,
    force_local: bool = False,
) -> list[RunnerResult]:
    """Execute a reference solution against a batch of test cases.

    Uses Judge0 backend when the ``JUDGE0_URL`` env var is set and the
    service is reachable; falls back to local subprocess otherwise.

    When ``force_local=True``, always uses local subprocess (skips Judge0).
    This is intended for test-case generation where the code is a trusted
    reference solution, not user-submitted code.

    Args:
        code: Python source (may be markdown-fenced).
        test_cases: List of dicts with ``input_args`` and ``expected_output``.
        timeout: Seconds per-case timeout.
        function_signature: e.g. ``"head: ListNode, k: int -> ListNode"``.
            用于把数组形式的入参还原成 ListNode/TreeNode 对象、并把结构化
            返回值序列化回数组（LeetCode 约定）。
        force_local: If True, skip Judge0 and always run locally.

    Returns:
        List of ``RunnerResult``, one per test case.
    """
    code = _extract_python_code(code)
    n = len(test_cases) or 1
    logger.info("▶ run_solution() — %d test cases, timeout=%.1fs, force_local=%s", n, timeout, force_local)

    # ── Try Judge0 backend when JUDGE0_URL is configured (skip if force_local) ──
    judge0_url = os.getenv("JUDGE0_URL")
    if judge0_url and not force_local:
        logger.info("  router → Judge0 (%s)", judge0_url)
        try:
            from code_tutor_agent.sandbox.judge0_client import submit_test_cases

            dict_results = submit_test_cases(
                code, test_cases, function_signature=function_signature,
            )
            if dict_results and dict_results[0].get("status") != "Judge Error":
                return [RunnerResult(
                    test_case_id=r["test_case_id"],
                    status=r["status"],
                    detail=r.get("detail", ""),
                    runtime_ms=r.get("runtime_ms", 0.0),
                    memory_kb=r.get("memory_kb", 0.0),
                    input_args=r.get("input_args"),
                    expected_output=r.get("expected_output", ""),
                    actual_output=r.get("actual_output", ""),
                ) for r in dict_results]
            else:
                logger.warning("Judge0 returned errors, falling back to local subprocess")
        except Exception as exc:
            logger.warning("Judge0 routing failed (%s), falling back to local", exc)

    # ── Fallback: local subprocess ──
    logger.info("  router → local subprocess")
    harness = _build_harness(code, test_cases, function_signature)

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8")
    try:
        tmp.write(harness)
        tmp.close()

        proc = subprocess.run(
            [sys.executable, tmp.name],
            capture_output=True,
            text=True,
            timeout=timeout + HARNESS_TIMEOUT,
            env=_build_subprocess_env(),
        )

        results: list[RunnerResult] = []
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT:"):
                data = json.loads(line[len("RESULT:"):])
                results.append(RunnerResult(**data))

        # If harness produced no structured output, fallback
        if not results and proc.returncode != 0:
            detail = _clean_error(proc.stderr[:500])
            return [RunnerResult(i, "Runtime Error", detail) for i in range(n)]
        if not results:
            return [RunnerResult(i, "Judge Error", "No structured output") for i in range(n)]

        return results

    except subprocess.TimeoutExpired:
        return [RunnerResult(i, "TLE", f"timed out after {timeout}s") for i in range(n)]
    except Exception as exc:
        logger.error("Exception: %s", exc)
        return [RunnerResult(i, "Runtime Error", str(exc)) for i in range(n)]
    finally:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)


def _build_harness(code: str, test_cases: list[dict], function_signature: str | None = None) -> str:
    """Build a standalone Python script that runs test cases against the code.

    Injects LeetCode types (from typing import *, TreeNode, ListNode, Node)
    into the global namespace before the user code, and — when a
    ``function_signature`` is provided — converts array-form arguments into
    ListNode/TreeNode objects and serialises structured return values back
    to arrays (LeetCode convention).
    """
    from code_tutor_agent.sandbox.ds import INJECT_PROLOGUE
    from code_tutor_agent.sandbox import struct_convert
    from code_tutor_agent.sandbox.input_generator import parse_signature

    # 解析签名，得到 (参数类型列表, 返回类型)，用于按类型还原/序列化
    param_types: list[str] = []
    return_type = ""
    if function_signature:
        params, return_type = parse_signature(function_signature)
        param_types = [t for _, t in params]

    tc_json = json.dumps(test_cases)
    struct_src = struct_convert.HARNESS_STRUCT_SRC
    param_types_json = json.dumps(param_types)
    return_type_json = json.dumps(return_type)

    return f"""\
{INJECT_PROLOGUE}
{struct_src}
import ast, json, sys, time, inspect
import logging

logger = logging.getLogger(__name__)


# --- User / reference code ---
{code}

# --- Test cases ---
test_cases = json.loads({tc_json!r})

_param_types = {param_types_json}
_return_type = {return_type_json}

def _eval_arg(s):
    # 优先 json.loads：LeetCode 测试用例入参是 JSON 字符串，空节点用 null 表示。
    # ast.literal_eval 只认 Python 字面量（None），遇 null 会抛错并退回原字符串，
    # 导致 _cta_tree_from_list 把整个字符串当成一个节点 → 树/链表题判错。
    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        return ast.literal_eval(s)
    except Exception:
        return s

def _fmt(val):
    if isinstance(val, (list, tuple)):
        return json.dumps(list(val), separators=(",", ":"))
    if isinstance(val, set):
        return json.dumps(sorted(val), separators=(",", ":"))
    return str(val)

# Discover the first public method on Solution
sol = Solution()
members = inspect.getmembers(sol, predicate=inspect.ismethod)
public = [(n, fn) for n, fn in members if not n.startswith('_')]
if not public:
    print('RESULT: ' + json.dumps({{"test_case_id": -1, "status": "Runtime Error", "detail": "no public method"}}))
    sys.exit(0)

method_name, method_fn = public[0]

for idx, tc in enumerate(test_cases):
    args = []
    _cta_first_tree = None
    for i, a in enumerate(tc['input_args']):
        _t = _param_types[i] if i < len(_param_types) else ""
        _raw = _eval_arg(a)
        # TreeNode 节点引用处理：如果参数是单值而非数组，说明是 p、q 这类
        # 需要从第一个树中按值查找的节点引用。LeetCode 的树问题中，
        # 第一个 TreeNode 参数是树根（数组输入），后续 TreeNode 是节点引用。
        if "TreeNode" in _t.replace("Optional[", "").rstrip("]"):
            if isinstance(_raw, list):
                _val = _cta_tree_from_list(_raw)
                if _cta_first_tree is None:
                    _cta_first_tree = _val
                args.append(_val)
            else:
                # 单值 → 从已建树中按值查找节点
                _val = _cta_find_node_by_value(_cta_first_tree, _raw) if _cta_first_tree else None
                args.append(_val)
        else:
            args.append(_cta_coerce_arg(_raw, _t))
    expected = tc['expected_output']
    start = time.perf_counter()
    try:
        result = method_fn(*args)
        elapsed = (time.perf_counter() - start) * 1000
        if _cta_is_void_result(result, _return_type):
            # 原地修改型：读回首个可变入参（LeetCode 约定被修改对象即 args[0]）
            actual = _fmt(args[0]) if args and isinstance(args[0], (list, dict, set)) else _fmt(None)
        else:
            actual = _fmt(_cta_coerce_result(result, _return_type))
        # 无参考答案（expected 为空）-> 不判定，标记 Skipped 并回传实际输出。
        # 两个用途：① 后台用例生成借此拿到参考解输出，作为权威 expected；
        # ② 判题侧跳过这类用例，避免拿空 expected 与正确输出比对而误判 WA。
        if expected is None or (isinstance(expected, str) and expected.strip() == ""):
            print('RESULT: ' + json.dumps({{"test_case_id": idx, "status": "Skipped", "detail": actual, "runtime_ms": round(elapsed, 2), "input_args": tc['input_args'], "expected_output": "", "actual_output": actual}}))
            continue
        # Normalise expected: parse JSON string to compare by value
        try:
            exp_val = json.loads(expected) if isinstance(expected, str) else expected
            exp_fmt = _fmt(exp_val)
        except (json.JSONDecodeError, TypeError, ValueError):
            exp_fmt = _fmt(expected)
        if actual == exp_fmt:
            print('RESULT: ' + json.dumps({{"test_case_id": idx, "status": "Passed", "detail": actual, "runtime_ms": round(elapsed, 2), "input_args": tc['input_args'], "expected_output": exp_fmt, "actual_output": actual}}))
        else:
            print('RESULT: ' + json.dumps({{"test_case_id": idx, "status": "Wrong Answer", "detail": f"expected={{exp_fmt}} got={{actual}}", "runtime_ms": round(elapsed, 2), "input_args": tc['input_args'], "expected_output": exp_fmt, "actual_output": actual}}))
    except Exception as exc:
        logger.error("Exception: %s", exc)
        elapsed = (time.perf_counter() - start) * 1000
        print('RESULT: ' + json.dumps({{"test_case_id": idx, "status": "Runtime Error", "detail": str(exc)[:200], "runtime_ms": round(elapsed, 2), "input_args": tc['input_args'], "expected_output": "", "actual_output": ""}}))
"""


def run_adversarial_check(
    brute_code: str,
    scale_spec: dict | None,
    timeout: float = TIMEOUT_SECONDS,
) -> RunnerResult | None:
    """Run brute_solution on a large adversarial input -> expect TLE."""
    if not brute_code or not scale_spec:
        return None

    n = scale_spec.get("n", 100_000)
    data_type = scale_spec.get("data_type", "int")
    scale_description = scale_spec.get("scale_description", "")
    large_case = _build_adversarial_case(n, data_type, scale_description)
    if not large_case:
        return None

    results = run_solution(brute_code, [large_case], timeout=timeout)
    return results[0] if results else None


def _build_adversarial_case(n: int, data_type: str, scale_description: str) -> dict | None:
    """Generate one large adversarial test case for O(n^2) brute force."""
    import random as rnd
    rnd.seed(42)

    n_actual = min(n, 20_000)
    target = 999999999
    a, b = 123456789, target - 123456789

    if "random" in scale_description.lower() or "分布" in scale_description:
        vals = [i * 2 + 1 for i in range(n_actual - 2)]
    elif "正负" in scale_description or "mixed" in scale_description.lower():
        vals = [(i * 2 + 1) * (1 if i % 2 == 0 else -1) for i in range(n_actual - 2)]
    else:
        vals = list(range(1, n_actual - 1))

    vals.append(a)
    vals.append(b)

    arr_str = "[" + ",".join(str(v) for v in vals) + "]"
    return {
        "input_args": [arr_str, str(target)],
        "expected_output": f"[{n_actual - 2}, {n_actual - 1}]",
    }