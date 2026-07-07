"""Tutor agent: reviews student code by calling the MCP judge tool."""

import os
import sys
import json

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from code_tutor_agent.memory import save_knowledge
from pydantic import BaseModel, Field
from typing import List

logger = logging.getLogger(__name__)


# Absolute path to the MCP server script
MCP_SERVER_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "mcp_server.py")
)


async def review_student_code(
    problem: dict,
    student_code: str,
    model_alias: str = "agnes",
) -> str:
    """Judge student code and return teacher-style feedback.

    Args:
        problem: A problem dict (e.g. from ``Problem.model_dump()``).
        student_code: The student's Python source code as a string.
        model_alias: Currently unused (kept for future extensibility).

    Returns:
        The agent's final message content (evaluation + commentary).
    """
    llm = ChatOpenAI(
        model=os.getenv("AGNES_MODEL"),
        base_url=os.getenv("AGNES_BASE_URL"),
        api_key=os.getenv("AGNES_API_KEY"),
        temperature=0.3,
    )

    client = MultiServerMCPClient({
        "code-tutor": {
            "command": sys.executable,
            "args": [MCP_SERVER_PATH],
            "transport": "stdio",
        }
    })
    tools = await client.get_tools()
    agent = create_agent(llm, tools)

    test_cases_json = json.dumps(problem["test_cases"], ensure_ascii=False)
    message = (
        "你是一个编程辅导老师。学生提交了以下代码，请你：\n"
        "1. 调用 judge_code 工具来评测这段代码\n"
        "2. 根据评测结果，给学生写一段简短的评语（指出做对了/做错了哪里，给出改进建议）\n\n"
        f"## 题目信息\n{problem['description']}\n"
        f"## 测试用例（JSON格式）\n{test_cases_json}\n"
        f"## 学生代码\n```\n{student_code}\n```"
    )

    result = await agent.ainvoke({"messages": [("user", message)]})
    feedback = result["messages"][-1].content

    # ---- Knowledge consolidation ----
    print("\n📝 辅导老师正在总结知识点...")
    llm_summary = ChatOpenAI(
        model=os.getenv("AGNES_MODEL"),
        base_url=os.getenv("AGNES_BASE_URL"),
        api_key=os.getenv("AGNES_API_KEY"),
        temperature=0.3,
    )

    summary_prompt = ChatPromptTemplate.from_messages([
        ("system", (
            "你是编程教学知识库的管理员。请根据题目和辅导反馈，提取核心知识点和易错点。"
            "以 JSON 格式返回，必须包含以下字段：\n"
            "- entity: 知识点名称（如 \"两数之和-哈希表解法\"）\n"
            "- entity_type: 类型（如 \"Algorithm\"）\n"
            "- observations: 易错点列表（如 [\"返回下标顺序不能反\", \"需处理数组为空的情况\"]）\n"
            "只返回 JSON，不要其他文字。"
        )),
        ("human", (
            "题目标题: {title}\n"
            "题目描述: {description}\n"
            "辅导反馈: {feedback}"
        )),
    ])

    class KnowledgeSummary(BaseModel):
        entity: str = Field(description="知识点名称")
        entity_type: str = Field(description="类型，如 Algorithm/Concept/CommonMistake")
        observations: List[str] = Field(description="易错点列表")

    structured_llm = llm_summary.with_structured_output(KnowledgeSummary)
    chain = summary_prompt | structured_llm
    summary_result_obj = await chain.ainvoke({
        "title": problem["title"],
        "description": problem["description"],
        "feedback": feedback,
    })
    summary_result = summary_result_obj.model_dump()

    save_knowledge(
        summary_result.get("entity", problem["title"]),
        summary_result.get("entity_type", "Concept"),
        summary_result.get("observations", []),
    )

    return feedback
