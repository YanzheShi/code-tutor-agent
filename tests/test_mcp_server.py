"""
验证 3：MCP Server 能启动并被调用
"""
import sys
import os
import asyncio
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from mcp.server.fastmcp import FastMCP
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


# 1. 定义 MCP Server
mcp = FastMCP("test-server")


@mcp.tool()
def hello_mcp(name: str) -> str:
    """一个简单的测试工具，返回问候语"""
    return f"Hello {name}, MCP is working!"


@mcp.tool()
def add(a: int, b: int) -> int:
    """加法工具"""
    return a + b


# 2. 定义测试用的 Client
@pytest.mark.asyncio
async def test_mcp_client():
    print("=" * 60)
    print("  可行性验证 3：MCP Server 启动与调用测试")
    print("=" * 60)

    # 通过 stdio 启动当前的 Python 脚本作为 Server
    server_params = StdioServerParameters(
        command=sys.executable,  # 当前 Python 解释器
        args=[os.path.abspath(__file__), "--run-server"],  # 传参让脚本以 server 模式启动
    )

    print("\n[1/3] 正在通过 stdio 启动 MCP Server...")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("✅ MCP Server 启动并握手成功！")

            print("\n[2/3] 正在获取可用工具列表...")
            tools = await session.list_tools()
            tool_names = [t.name for t in tools.tools]
            print(f"✅ 发现工具: {tool_names}")

            expected_tools = {"hello_mcp", "add"}
            if not expected_tools.issubset(set(tool_names)):
                print(f"❌ 工具列表不完整，期望包含 {expected_tools}")
                return False

            print("\n[3/3] 正在调用 hello_mcp 工具...")
            result = await session.call_tool("hello_mcp", {"name": "Developer"})

            if result.content and len(result.content) > 0:
                output_text = result.content[0].text
                print(f"✅ 工具返回结果: {output_text}")
                if "MCP is working" in output_text:
                    print("\n" + "=" * 60)
                    print("  ✅ 验证 3 通过！MCP 环境完全可用。")
                    print("=" * 60)
                    return True

    print("\n❌ 验证 3 失败。")
    return False


if __name__ == "__main__":
    # 如果带了 --run-server 参数，就作为 Server 运行；否则运行测试 Client
    if "--run-server" in sys.argv:
        print("MCP Server 正在运行 (stdio)...", file=sys.stderr)
        mcp.run()
    else:
        asyncio.run(test_mcp_client())
