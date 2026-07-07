"""MCP Server: exposes the judge tool via MCP protocol."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from mcp.server.fastmcp import FastMCP
from code_tutor_agent.tools.judge import run_code

mcp = FastMCP("code-tutor-judge")


@mcp.tool()
def judge(code: str, test_cases: list) -> list:
    """
    Execute user code (class Solution style) against test cases.

    Args:
        code: User's Python code (typically a class Solution definition).
        test_cases: List of dicts with "input_args" and "expected_output" keys.

    Returns:
        List of result dicts with "test_case_id", "status", and optional "detail".
    """
    return run_code(code, test_cases)


if __name__ == "__main__":
    mcp.run()
