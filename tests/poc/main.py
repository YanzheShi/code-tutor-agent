"""CLI entry point for Code Tutor Agent.

Interactive loop: generate / pick a problem → submit your code → get AI feedback.
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

# Load .env before anything that reads env vars
load_dotenv()

# Fix Windows console encoding for emoji support
if sys.platform == "win32":
    import codecs

    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer, "strict")
    sys.stderr = codecs.getwriter("utf-8")(sys.stderr.buffer, "strict")

# Ensure the project root is on sys.path so imports resolve regardless of cwd
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from code_tutor_agent.db.database import init_db, save_problem, get_problem_by_id
from code_tutor_agent.agents.problem_generator import generate_problem
from code_tutor_agent.agents.tutor import review_student_code

# Default LeetCode-style template shown to students
DEFAULT_TEMPLATE = """\
from typing import List


class Solution:
    def solve(self, nums: List[int], target: int) -> List[int]:
        # TODO: 在这里实现你的代码
        pass
"""


async def main():
    # Initialise DB tables on startup
    init_db()

    print("=" * 60)
    print("🤖 欢迎使用 AI 编程私教 (Code Tutor Agent)")
    print("输入 'quit' 退出程序")
    print("=" * 60)

    while True:
        print("\n请选择模式：")
        print("1. 让 AI 出一道新题")
        print("2. 做题库已有的题 (输入题号)")
        choice = input("请输入 1 或 2: ").strip()

        if choice == "quit":
            break

        problem_dict = None

        if choice == "1":
            topic = input("请输入要练习的知识点（如 数组、动态规划）: ").strip()
            if topic.lower() == "quit":
                break

            difficulty = input("请输入难度: ").strip().lower()
            if difficulty not in ("easy", "medium", "hard"):
                print("难度必须是 easy, medium 或 hard，默认使用 easy")
                difficulty = "easy"

            print(
                f"\n⏳ 正在为你生成「{topic}」的 {difficulty} 难度题目，请稍候..."
            )
            problem = generate_problem(topic, difficulty)
            problem_dict = problem.model_dump()

            # Persist to DB
            problem_id = save_problem(problem_dict)
            problem_dict["id"] = problem_id

            print(f"\n📝 新题目已生成 (题库 ID: {problem_id}):")
            print(f"标题: {problem_dict['title']}")
            print(f"描述:\n{problem_dict['description']}")
            print(f"测试用例共 {len(problem_dict['test_cases'])} 个。")

        elif choice == "2":
            pid_str = input("请输入题号: ").strip()
            try:
                pid = int(pid_str)
                problem_dict = get_problem_by_id(pid)
                if problem_dict:
                    print(f"\n📝 获取题目成功 (题库 ID: {problem_dict['id']}):")
                    print(f"标题: {problem_dict['title']}")
                    print(f"描述:\n{problem_dict['description']}")
                else:
                    print("❌ 找不到该题号，请重新选择。")
                    continue
            except ValueError:
                print("❌ 题号必须是数字。")
                continue
        else:
            print("❌ 无效输入。")
            continue

        # ---- Step 2: submit student code ----
        print("\n--- 步骤 2: 提交你的代码 ---")
        print("请在 my_solution.py 中编写你的代码（使用 class Solution 风格）。")
        print("\n模板代码：")
        print(DEFAULT_TEMPLATE)
        file_path = input("请输入你的代码文件路径 (默认 my_solution.py): ").strip()
        if not file_path:
            file_path = "my_solution.py"

        # Resolve relative paths against the project root
        if not os.path.isabs(file_path):
            file_path = os.path.join(_project_root, file_path)

        if not os.path.exists(file_path):
            print(f"❌ 找不到文件 {file_path}，跳过本轮。")
            continue

        with open(file_path, "r", encoding="utf-8") as f:
            student_code = f.read()

        print("\n⏳ 辅导老师正在批改你的代码并生成点评...")
        feedback = await review_student_code(problem_dict, student_code)

        print("\n" + "=" * 60)
        print("🎓 辅导老师点评:")
        print("=" * 60)
        print(feedback)
        print("=" * 60)


def cli():
    """Entry point for ``uv run code-tutor`` CLI mode."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n再见！")


if __name__ == "__main__":
    cli()
