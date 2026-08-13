"""Judge0 MCP Server — exposes coding-judge tools via the MCP protocol.

Tools:
  - ``judge_run_code``       — Execute arbitrary source code, return stdout/stderr.
  - ``judge_run_test_cases`` — Batch-judge LeetCode-style code against test cases.
  - ``judge_check_health``   — Check Judge0 service health.
  - ``judge_check_languages``— List available languages.

All submissions pass ``enable_per_process_and_thread_time_limit=true`` and
``enable_per_process_and_thread_memory_limit=true`` so isolate uses soft
limits (``-m``/``-t``) compatible with WSL's cgroup v2.

Start::

    uv run python -m code_tutor_agent.mcp.judge0_server

Integration (LangChain / LangGraph)::

    from langchain_mcp_adapters.client import MultiServerMCPClient
    client = MultiServerMCPClient({
        "judge0": {
            "command": "uv",
            "args": ["run", "python", "-m", "code_tutor_agent.mcp.judge0_server"],
            "transport": "stdio",
        }
    })
    tools = await client.get_tools()
"""

from __future__ import annotations

import json
import logging

from mcp.server.fastmcp import FastMCP

from code_tutor_agent.sandbox.judge0_client import (
    run_code,
    submit_test_cases,
    check_health,
    list_languages,
    SandboxNotExecuted,
)

logger = logging.getLogger(__name__)

server = FastMCP("judge0-server", log_level="INFO")


# ── Tool 1: Execute arbitrary code ──


@server.tool()
async def judge_run_code(
    source_code: str,
    language: str = "python",
    stdin: str = "",
) -> str:
    """Execute a code snippet and return stdout, stderr, and execution stats.

    Use this when you (the LLM) want to test a code snippet to verify it
    compiles and runs correctly — for debugging, exploration, or validating
    a fix before suggesting it to the student.

    Args:
        source_code: The complete source code to execute.
        language: Programming language name (default "python").
        stdin: Standard input to pipe to the program.

    Returns:
        JSON string with ``stdout``, ``stderr``, ``status``, ``time_ms``, ``memory_kb``.
    """
    lang_map = {"python": 71, "javascript": 63, "cpp": 54, "c": 50, "java": 62, "go": 60}
    lang_id = lang_map.get(language.lower(), 71)

    try:
        result = run_code(source_code, stdin=stdin, language_id=lang_id)
    except (SandboxNotExecuted, RuntimeError) as exc:
        return json.dumps({
            "verdict": "NO_RUN",
            "status": "sandbox_unavailable",
            "error": str(exc),
            "message": "代码验证沙箱当前不可用（提交后未执行或网络不可达），请基于知识回答。",
        }, ensure_ascii=False)
    return json.dumps({
        "stdout": result.stdout,
        "stderr": result.stderr,
        "compile_output": result.compile_output,
        "status": result.status_desc,
        "verdict": result.verdict(),
        "time_ms": result.runtime_ms(),
        "memory_kb": result.memory_kilobytes,
        "token": result.token,
    }, ensure_ascii=False)


# ── Tool 2: Batch judge against test cases ──


@server.tool()
async def judge_run_test_cases(
    source_code: str,
    test_cases_json: str,
    language: str = "python",
) -> str:
    """Run a LeetCode-style solution against a batch of test cases.

    Use this when a student submits code and you want to judge it against
    the problem's test cases.  The code must define a ``class Solution``
    with at least one public method.

    Args:
        source_code: The student's code (``class Solution: ...``).
        test_cases_json: JSON string — array of ``{"input_args": [...], "expected_output": "..."}``.
        language: Programming language (default "python").

    Returns:
        JSON string: ``{"results": [...], "summary": {"total": N, "passed": M}}``.
    """
    test_cases = json.loads(test_cases_json) if isinstance(test_cases_json, str) else test_cases_json
    lang_map = {"python": 71, "javascript": 63, "cpp": 54, "c": 50, "java": 62, "go": 60}
    lang_id = lang_map.get(language.lower(), 71)

    results = submit_test_cases(source_code, test_cases, language_id=lang_id)
    passed = sum(1 for r in results if r.get("status") == "Passed")

    return json.dumps({
        "results": results,
        "summary": {
            "total": len(results),
            "passed": passed,
            "all_passed": passed == len(results),
        },
    }, ensure_ascii=False)


# ── Tool 3: Health check ──


@server.tool()
async def judge_check_health() -> str:
    """Check if the Judge0 backend is alive and how many workers are running.

    Returns:
        JSON string with ``status``, ``workers_alive``, ``api_version``.
    """
    h = check_health()
    return json.dumps(h, ensure_ascii=False)


# ── Tool 4: List languages ──


@server.tool()
async def judge_check_languages() -> str:
    """List all programming languages supported by this Judge0 instance.

    Returns:
        JSON string — array of ``{"id": int, "name": str}``.
    """
    langs = list_languages()
    return json.dumps(langs, ensure_ascii=False)


# ── Entry point ──


def main():
    logging.basicConfig(level=logging.INFO, force=True)
    server.run()


if __name__ == "__main__":
    main()