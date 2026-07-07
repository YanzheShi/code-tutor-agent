"""Code-Tutor MCP Server — used by the tutor agent during conversation.

Provides tools the LLM tutor can call to judge student code:

- ``judge_code``         — (primary) run a LeetCode-style solution against test cases
- ``judge_run_code``     — execute arbitrary code snippet
- ``judge_check_health`` — check Judge0 backend health

All submissions go through Judge0 (WSL Docker) with cgroup-v2-compatible
settings (``enable_per_process_and_thread_time_limit=true``).

Start (normally spawned by the tutor agent):

    uv run python -m code_tutor_agent.mcp_server
"""

from __future__ import annotations

import json
import logging

from mcp.server.fastmcp import FastMCP

from code_tutor_agent.sandbox.judge0_client import (
    run_code,
    submit_test_cases,
    check_health,
)

logger = logging.getLogger(__name__)

server = FastMCP("code-tutor-server", log_level="INFO")


@server.tool()
async def judge_code(
    source_code: str,
    test_cases_json: str,
) -> str:
    """Run a LeetCode-style solution against a batch of test cases.

    Use this when a student submits code that needs to be judged — it
    runs every test case through the submitted code and reports pass/fail
    per case plus a summary.

    Args:
        source_code: The student's ``class Solution: ...`` Python code.
        test_cases_json: JSON array of ``[{"input_args": [...], "expected_output": "..."}]``.

    Returns:
        JSON with ``results`` (per-case) and ``summary`` (total/passed/all_passed).
    """
    test_cases = json.loads(test_cases_json) if isinstance(test_cases_json, str) else test_cases_json
    results = submit_test_cases(source_code, test_cases)
    passed = sum(1 for r in results if r.get("status") == "Passed")
    return json.dumps({
        "results": results,
        "summary": {
            "total": len(results),
            "passed": passed,
            "all_passed": passed == len(results),
        },
    }, ensure_ascii=False)


@server.tool()
async def judge_run_code(
    source_code: str,
    stdin: str = "",
) -> str:
    """Execute an arbitrary code snippet and return stdout/stderr/stats.

    Use this to test a code snippet, debug an algorithm, or verify a fix
    during tutoring.

    Args:
        source_code: The complete Python source code.
        stdin: Optional standard input to pipe to the program.

    Returns:
        JSON with ``stdout``, ``stderr``, ``status``, ``time_ms``, ``memory_kb``.
    """
    result = run_code(source_code, stdin=stdin)
    return json.dumps({
        "stdout": result.stdout,
        "stderr": result.stderr,
        "status": result.status_desc,
        "verdict": result.verdict(),
        "time_ms": result.runtime_ms(),
        "memory_kb": result.memory_kilobytes,
    }, ensure_ascii=False)


@server.tool()
async def judge_check_health() -> str:
    """Check if the Judge0 backend (WSL Docker) is alive.

    Returns:
        JSON with ``workers_alive``, ``api_version``, ``status``.
    """
    h = check_health()
    return json.dumps(h, ensure_ascii=False)


def main():
    logging.basicConfig(level=logging.INFO, force=True)
    server.run()


if __name__ == "__main__":
    main()