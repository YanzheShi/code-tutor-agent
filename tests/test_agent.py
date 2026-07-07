"""
验证 4：LangChain Agent 调用 MCP 工具进行自主判题
运行：python tests/test_agent.py
"""
import os
import sys
import asyncio
import json
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain.agents import create_agent as create_react_agent

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

PROBLEM_CONTEXT = """
题目：两数之和
描述：给定一个整数数组 nums 和一个整数目标值 target，在数组中找出和为 target 的两个整数，返回它们的下标。
输入格式：第一行是数组（如 [2, 7, 11, 15]），第二行是目标值（如 9）
输出格式：下标列表（如 [0, 1]）
"""

TEST_CASES = [
    {"input": "[2, 7, 11, 15]\n9", "expected_output": "[0, 1]"},
    {"input": "[3, 2, 4]\n6", "expected_output": "[1, 2]"},
    {"input": "[3, 3]\n6", "expected_output": "[0, 1]"},
]

# 正确代码
STUDENT_CODE_CORRECT = """\
def solve():
    import sys
    data = sys.stdin.read().strip().split('\\n')
    nums_str = data[0].strip()
    nums = [int(x.strip()) for x in nums_str[1:-1].split(',')]
    target = int(data[1].strip())
    seen = {}
    for i, num in enumerate(nums):
        complement = target - num
        if complement in seen:
            print([seen[complement], i])
            return
        seen[num] = i
solve()
"""

# 错误代码（返回顺序反了）
STUDENT_CODE_WRONG = STUDENT_CODE_CORRECT.replace(
    "print([seen[complement], i])", "print([i, seen[complement]])"
)


# ---------------------------------------------------------------------------
# Agent test
# ---------------------------------------------------------------------------

async def run_agent_test():
    print("=" * 60)
    print("  可行性验证 4：Agent 自主调用 MCP 判题工具")
    print("=" * 60)

    # 1. 初始化模型
    llm = ChatOpenAI(
        model=os.getenv("SENSENOVA_MODEL1"),
        base_url=os.getenv("SENSENOVA_BASE_URL"),
        api_key=os.getenv("SENSENOVA_API_KEY"),
        temperature=0.3,
    )
    print("✅ 模型初始化成功")

    # 2. 连接 MCP Server，加载工具
    mcp_server_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "src", "code_tutor_agent", "poc/mcp_server.py")
    )
    client = MultiServerMCPClient(
        {
            "code-tutor": {
                "command": sys.executable,
                "args": [mcp_server_path],
                "transport": "stdio",
            }
        }
    )
    tools = await client.get_tools()
    print(f"✅ 从 MCP Server 加载到 {len(tools)} 个工具: {[t.name for t in tools]}")

    # 3. 构建 Agent
    agent = create_react_agent(llm, tools)
    print("✅ Agent 构建成功")

    # -----------------------------------------------------------------------
    # 场景 A：正确的代码
    # -----------------------------------------------------------------------
    print("\n" + "-" * 60)
    print("📋 场景 A：学生提交了正确的代码")
    print("-" * 60)

    test_cases_json = json.dumps(TEST_CASES, ensure_ascii=False)
    message_a = (
        "你是一个编程辅导老师。学生提交了以下代码，请你：\n"
        "1. 调用 judge_code 工具来评测这段代码\n"
        "2. 根据评测结果，给学生写一段简短的评语\n\n"
        f"## 题目信息\n{PROBLEM_CONTEXT}\n"
        f"## 测试用例（JSON格式）\n{test_cases_json}\n"
        f"## 学生代码\n```\n{STUDENT_CODE_CORRECT}\n```"
    )

    print("\n🤖 Agent 正在思考...")
    result_a = await agent.ainvoke({"messages": [("user", message_a)]})
    final_message_a = result_a["messages"][-1]
    print(f"\n📝 Agent 评语:\n{final_message_a.content}")

    # -----------------------------------------------------------------------
    # 场景 B：错误的代码
    # -----------------------------------------------------------------------
    print("\n" + "-" * 60)
    print("📋 场景 B：学生提交了错误的代码（返回顺序反了）")
    print("-" * 60)

    message_b = (
        "你是一个编程辅导老师。学生提交了以下代码，请你：\n"
        "1. 调用 judge_code 工具来评测这段代码\n"
        "2. 根据评测结果，给学生写一段简短的评语\n\n"
        f"## 题目信息\n{PROBLEM_CONTEXT}\n"
        f"## 测试用例（JSON格式）\n{test_cases_json}\n"
        f"## 学生代码\n```\n{STUDENT_CODE_WRONG}\n```"
    )

    print("\n🤖 Agent 正在思考...")
    result_b = await agent.ainvoke({"messages": [("user", message_b)]})
    final_message_b = result_b["messages"][-1]
    print(f"\n📝 Agent 评语:\n{final_message_b.content}")

    # -----------------------------------------------------------------------
    # 总结
    # -----------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("  ✅ 验证 4 完成！Agent 能自主调用 MCP 工具进行判题。")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_agent_test())
