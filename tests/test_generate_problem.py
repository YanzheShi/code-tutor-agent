"""
验证 1：大模型结构化输出稳定性测试
运行：python tests/test_generate_problem.py
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from langchain_core.prompts import ChatPromptTemplate
from code_tutor_agent.config import get_llm  # 👈 引入配置中心
from code_tutor_agent.models.problem import Problem
from code_tutor_agent.prompts.generate_problem import (
    GENERATE_PROBLEM_SYSTEM,
    GENERATE_PROBLEM_USER,
)


def _run_structured_output(llm, topic: str, difficulty: str) -> tuple[bool, Problem | None, str]:
    # 关键：用 with_structured_output 约束输出格式
    structured_llm = llm.with_structured_output(Problem)
    prompt = ChatPromptTemplate.from_messages([
        ("system", GENERATE_PROBLEM_SYSTEM),
        ("human", GENERATE_PROBLEM_USER),
    ])
    chain = prompt | structured_llm
    try:
        problem = chain.invoke({"topic": topic, "difficulty": difficulty})
        return True, problem, ""
    except Exception as e:
        return False, None, str(e)

def validate_problem_quality(problem: Problem) -> list[str]:
    issues = []
    if len(problem.test_cases) < 5: issues.append(f"测试用例不足 5 个，只有 {len(problem.test_cases)} 个")
    hidden_count = sum(1 for tc in problem.test_cases if tc.is_hidden)
    if hidden_count == 0: issues.append("没有隐藏测试用例")
    if problem.difficulty.lower() not in ("easy", "medium", "hard"): issues.append(f"难度字段不合法: {problem.difficulty}")
    if not problem.solution.strip(): issues.append("参考解法为空")
    if "def " not in problem.solution and "class " not in problem.solution: issues.append("参考解法可能不是合法的 Python 代码（缺少 def/class）")
    return issues


if __name__ == "__main__":
    # 👈 在这里选择你要测试的用途！
    PURPOSE = "problem"

    # 拿到模型实例
    llm = get_llm(purpose=PURPOSE)
    print(f"当前测试用途: {PURPOSE}")

    print("=" * 60)
    print("  可行性验证 1：大模型结构化输出稳定性测试")
    print("=" * 60)

    test_configs = [
        ("数组", "easy"),
        # ("双指针", "easy"),
    ]

    success_count = 0
    total_count = len(test_configs)
    all_problems = []

    for i, (topic, difficulty) in enumerate(test_configs, 1):
        print(f"\n[{i}/{total_count}] 正在生成: {topic} - {difficulty}")
        success, problem, error = _run_structured_output(llm, topic, difficulty)

        if success:
            print(f"  ✅ 结构化输出解析成功")
            print(f"     题目: {problem.title}")
            print(f"     用例数: {len(problem.test_cases)}")

            issues = validate_problem_quality(problem)
            if issues:
                print(f"  ⚠️  质量检查发现问题:")
                for issue in issues: print(f"     - {issue}")
            else:
                print(f"  ✅ 质量检查通过")
                success_count += 1
                all_problems.append(problem)
        else:
            print(f"  ❌ 解析失败: {error[:300]}")

    print("\n" + "=" * 60)
    print(f"  结果: {success_count}/{total_count} 完全通过")
    print("=" * 60)

    if all_problems:
        sample = all_problems[0]
        os.makedirs("data", exist_ok=True)
        with open("data/sample_problem.json", "w", encoding="utf-8") as f:
            f.write(sample.model_dump_json(indent=2))
        print(f"\n样本题目已保存到: data/sample_problem.json")
