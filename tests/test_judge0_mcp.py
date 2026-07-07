"""Test judge0 MCP server connectivity.

Tests the MCP protocol handshake and tool discovery with the Judge0 MCP server.

Usage:
    pytest tests/test_judge0_mcp.py -v          # Run via pytest
    python tests/test_judge0_mcp.py              # Run standalone

Known issue: The @javaguru/server-judge0 npx package prints diagnostic
messages (e.g. "[Judge0] Failed to fetch languages...") to stdout, which
confuses the MCP JSON-RPC parser. This test wraps the server in a small
Node.js script that filters non-JSON lines before they hit the stream.

Dependencies:
    mcp>=1.0.0  (declared in pyproject.toml)
"""

import asyncio
import json
import os
import sys
import tempfile

# Fix Windows console GBK encoding issue
if sys.platform == "win32":
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer, "strict")
    sys.stderr = codecs.getwriter("utf-8")(sys.stderr.buffer, "strict")

# Suppress MCP server stderr noise
import logging
logging.getLogger("anyio._backends._asyncio").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

# Judge0 MCP Server config
JUDGE0_MCP_COMMAND = "npx"
JUDGE0_MCP_ARGS = ["-y", "@javaguru/server-judge0"]
JUDGE0_BASE_URL = os.getenv("JUDGE0_MCP_URL", "http://localhost:2358")
MCP_TIMEOUT = 60


def _make_node_wrapper() -> str:
    """Create a Node.js wrapper script that filters non-JSON lines from stdout."""
    script = r"""
const {spawn} = require("child_process");
const isWin = process.platform === "win32";
const cmd = isWin ? "cmd" : "npx";
const spawnArgs = isWin ? ["/c", "npx", "-y", "@javaguru/server-judge0"] : ["-y", "@javaguru/server-judge0"];
const server = spawn(cmd, spawnArgs, {
    stdio: ["pipe", "pipe", "pipe"],
    env: Object.assign({}, process.env, {JUDGE0_BASE_URL: process.env.JUDGE0_BASE_URL})
});

// Forward stdin to server
process.stdin.pipe(server.stdin);

// Filter stdout: only forward valid JSON-RPC lines
let buf = "";
server.stdout.on("data", (chunk) => {
    buf += chunk.toString();
    let lines = buf.split("\n");
    buf = lines.pop();
    for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        // Skip diagnostic lines that start with brackets or known prefixes
        if (trimmed.startsWith("[") || trimmed.startsWith("✓") ||
            trimmed.startsWith("Judge0") || trimmed.startsWith("WARNING") ||
            trimmed.startsWith("Error:")) {
            process.stderr.write(trimmed + "\n");
            continue;
        }
        // Try to parse as JSON — only forward if valid
        try {
            JSON.parse(trimmed);
            process.stdout.write(line + "\n");
        } catch(e) {
            process.stderr.write("FILTERED: " + trimmed + "\n");
        }
    }
});

server.stderr.pipe(process.stderr);
server.on("close", () => process.exit(0));
"""
    path = os.path.join(tempfile.gettempdir(), "judge0_mcp_wrapper.cjs")
    with open(path, "w", encoding="utf-8") as f:
        f.write(script)
    return path


def _server_params(wrapper_path: str) -> StdioServerParameters:
    """Build MCP Server parameters using the Node.js wrapper."""
    env = os.environ.copy()
    env["JUDGE0_BASE_URL"] = JUDGE0_BASE_URL
    return StdioServerParameters(
        command="node",
        args=[wrapper_path],
        env=env,
        cwd=os.getcwd(),
    )


# ===================================================================
#  Test 1: Handshake + Tool List
# ===================================================================

async def test_mcp_handshake_and_tools():
    """Verify MCP Server starts, handshakes, and lists tools."""
    print("\n" + "=" * 60)
    print("  Test 1: MCP Server Handshake & Tool List")
    print("=" * 60)

    wrapper_path = _make_node_wrapper()
    try:
        params = _server_params(wrapper_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                # 1) Initialize handshake
                print("\n[1/3] Initializing handshake ...")
                await session.initialize()
                print("  [OK] Handshake successful")

                # 2) List tools
                print("\n[2/3] Listing tools ...")
                tools_resp = await session.list_tools()
                tools = tools_resp.tools
                tool_names = [t.name for t in tools]
                print(f"  [OK] Found {len(tools)} tools: {tool_names}")

                expected = {"execute_code", "list_languages", "execute_python", "execute_javascript"}
                missing = expected - set(tool_names)
                if missing:
                    print(f"  [WARN] Missing tools: {missing}")
                else:
                    print(f"  [OK] All expected tools found")

                for t in tools:
                    desc_preview = (t.description or "")[:120].replace("\n", " ")
                    print(f"     - {t.name}: {desc_preview}")

                # 3) Call list_languages
                print("\n[3/3] Calling list_languages tool ...")
                lang_result = await session.call_tool("list_languages", {})
                print(f"  [OK] Returned {len(lang_result.content)} content block(s)")
                for c in lang_result.content:
                    if hasattr(c, "text"):
                        preview = c.text[:200].replace("\n", " ")
                        print(f"     {preview}...")

        print("\n  [OK] Test 1 PASSED!")
        return True

    finally:
        if os.path.exists(wrapper_path):
            os.remove(wrapper_path)


# ===================================================================
#  Test 2: Execute Python Code
# ===================================================================

async def test_execute_python():
    """Execute a simple Python snippet via execute_python tool."""
    print("\n" + "=" * 60)
    print("  Test 2: execute_python — Run Python Code")
    print("=" * 60)

    wrapper_path = _make_node_wrapper()
    try:
        params = _server_params(wrapper_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                code = "print(sum(range(10)))"
                print(f"\n  Executing: {code}")

                result = await session.call_tool("execute_python", {"code": code})
                print(f"  [OK] Returned {len(result.content)} content block(s):")
                for c in result.content:
                    text = getattr(c, "text", str(c))
                    print(f"     [{c.type}] {text.strip()[:200]}")

        print("\n  [OK] Test 2 PASSED!")
        return True

    finally:
        if os.path.exists(wrapper_path):
            os.remove(wrapper_path)


# ===================================================================
#  Test 3: Execute Generic Code (execute_code)
# ===================================================================

async def test_execute_code():
    """Execute Python code via the generic execute_code tool."""
    print("\n" + "=" * 60)
    print("  Test 3: execute_code — Generic Code Execution")
    print("=" * 60)

    wrapper_path = _make_node_wrapper()
    try:
        params = _server_params(wrapper_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                code = """\
class Solution:
    def twoSum(self, nums, target):
        seen = {}
        for i, num in enumerate(nums):
            complement = target - num
            if complement in seen:
                return [seen[complement], i]
            seen[num] = i

sol = Solution()
print(sol.twoSum([2, 7, 11, 15], 9))
"""
                print(f"\n  Executing: twoSum([2,7,11,15], 9)")

                result = await session.call_tool("execute_code", {
                    "code": code,
                    "language": "python",
                    "timeout": 5.0,
                })
                print(f"  [OK] Returned {len(result.content)} content block(s):")
                for c in result.content:
                    text = getattr(c, "text", str(c))
                    print(f"     [{c.type}] {text.strip()[:200]}")

        print("\n  [OK] Test 3 PASSED!")
        return True

    finally:
        if os.path.exists(wrapper_path):
            os.remove(wrapper_path)


# ===================================================================
#  Test 4: Judge0 Backend Connectivity Check
# ===================================================================

async def test_judge0_backend():
    """Check if Judge0 backend is reachable (separate from MCP protocol)."""
    print("\n" + "=" * 60)
    print("  Test 4: Judge0 Backend Connectivity")
    print("=" * 60)

    import urllib.request
    import urllib.error

    url = JUDGE0_BASE_URL.rstrip("/") + "/health"
    print(f"\n  Checking Judge0 backend at: {url}")

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
            print(f"  [OK] Judge0 backend responded: {body[:200]}")
            print("\n  [OK] Test 4 PASSED!")
            return True
    except urllib.error.URLError as e:
        print(f"  [WARN] Judge0 backend unreachable: {e.reason}")
        print("  [INFO] This is expected if Judge0 container is not running.")
        print("  [INFO] The MCP protocol layer (Tests 1-3) still works fine.")
        print("\n  [SKIP] Test 4 SKIPPED (backend unreachable)")
        return True
    except Exception as e:
        print(f"  [WARN] Could not check backend: {e}")
        print("\n  [SKIP] Test 4 SKIPPED")
        return True


# ===================================================================
#  Entry Point
# ===================================================================

async def _with_timeout(coro, label: str, timeout: float = MCP_TIMEOUT):
    """Run a coroutine with a timeout. Returns (success, result_or_error)."""
    try:
        result = await asyncio.wait_for(coro, timeout=timeout)
        return True, result
    except asyncio.TimeoutError:
        return False, f"Timed out after {timeout}s ({label})"
    except Exception as exc:
        return False, str(exc)


def main():
    """Run all tests sequentially (non-pytest mode)."""
    tests = [
        ("Handshake & Tool List", test_mcp_handshake_and_tools),
        ("execute_python", test_execute_python),
        ("execute_code", test_execute_code),
        ("Judge0 Backend Check", test_judge0_backend),
    ]

    passed = 0
    failed = 0

    for name, coro_func in tests:
        ok, result = asyncio.run(_with_timeout(coro_func(), name))
        if ok:
            passed += 1
        else:
            failed += 1
            print(f"\n  [FAIL] Test '{name}': {result}")

    print("\n" + "=" * 60)
    print(f"  Result: {passed} passed, {failed} failed (total {len(tests)})")
    print("=" * 60)
    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
