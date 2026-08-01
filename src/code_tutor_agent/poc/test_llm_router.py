"""POC：用真实 LLM 做 Skill 路由 — 不靠关键词匹配，让大模型自己选。

验证链路：
    1. 加载两个 SKILL.md（generate-array / generate-dp）
    2. 用 LLM 分析用户需求 → 返回最合适的 skill + 理由
    3. 用 LLM 调用选中的 skill → 生成题目
    4. 输出校验

运行方式（项目根目录）：
    uv run python -m src.code_tutor_agent.poc.test_llm_router

依赖：项目已有的 .env 中的 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.code_tutor_agent.poc.skill_parser import Skill, load_skill
from src.code_tutor_agent.config import get_llm

SKILLS_DIR = Path(__file__).resolve().parent / "skills"


def load_all_skills(skills_dir: Path) -> list[Skill]:
    """加载所有可用 skill。"""
    skills = []
    for d in sorted(skills_dir.iterdir()):
        if d.is_dir() and (d / "SKILL.md").exists():
            try:
                skills.append(load_skill(d.name, skills_dir))
                print(f"  [加载] {d.name}")
            except Exception as e:
                print(f"  [跳过] {d.name}: {e}")
    return skills


def build_skill_catalog(skills: list[Skill]) -> str:
    """把所有 skill 的描述、schema 字段、示例主题整理成目录字符串。"""
    lines = []
    for i, s in enumerate(skills, 1):
        lines.append(f"  [{i}] {s.name}")
        lines.append(f"      描述: {s.description}")
        if s.schema:
            props = s.schema.get("properties", {})
            required = s.schema.get("required", [])
            fields = [f"{k}({v.get('type','?')}){' *必填' if k in required else ''}" for k, v in props.items()]
            lines.append(f"      输出字段: {', '.join(fields[:8])}")
        if s.examples:
            ex = s.examples[0].get("input", {})
            lines.append(f"      示例输入: topic={ex.get('topic','')}, difficulty={ex.get('difficulty','')}")
    return "\n".join(lines)


def llm_route(skills: list[Skill], user_topic: str, user_difficulty: str, llm) -> dict:
    """用 LLM 分析用户需求，返回选中的 skill 名称和理由。

    Returns:
        {"skill_name": "generate-dp", "reason": "...", "topic": "...", "difficulty": "..."}
    """
    catalog = build_skill_catalog(skills)

    router_prompt = (
        "你是一个 Skill 路由专家。你的任务是根据用户的出题需求，从以下 skill 目录中选择最合适的一个。\n\n"
        "## 可用 Skill 目录\n\n"
        f"{catalog}\n\n"
        "## 用户需求\n\n"
        f"- 知识点：{user_topic}\n"
        f"- 难度：{user_difficulty}\n\n"
        "## 你的任务\n\n"
        "分析用户的需求，选择最合适的 skill。考虑以下因素：\n"
        "1. skill 的描述是否覆盖用户的知识点\n"
        "2. skill 的输出字段是否包含用户关心的内容\n"
        "3. 如果用户知识点是数组/哈希表/双指针，generate-array 最合适\n"
        "4. 如果用户知识点是动态规划/背包/状态转移，generate-dp 最合适\n"
        "5. 如果都不太匹配，选最接近的\n\n"
        "## 输出格式\n\n"
        "只输出 JSON，不要额外文字：\n"
        "```json\n"
        '{\n'
        '    "skill_name": "选中的 skill 名称",\n'
        '    "topic": "用户的知识点（可微调）",\n'
        '    "difficulty": "用户选的难度",\n'
        '    "reason": "选择理由（一句话）"\n'
        '}\n'
        "```"
    )

    print(f"\n  [LLM Router] 发送路由请求...")
    resp = llm.invoke([("human", router_prompt)])
    content = resp.content if hasattr(resp, "content") else str(resp)

    # 解析 JSON
    import re
    m = re.search(r"```(?:json)?\s*\n(.*?)```", content, re.DOTALL)
    if m:
        result = json.loads(m.group(1))
    else:
        result = json.loads(content)

    print(f"  [LLM Router] 选择: {result.get('skill_name')}")
    print(f"  [LLM Router] 理由: {result.get('reason')}")
    return result


def main():
    print("=" * 60)
    print("  POC：LLM 驱动的 Skill 路由")
    print("=" * 60)

    # 1. 加载所有 skill
    print("\n[Step 1] 加载 SKILL.md...")
    skills = load_all_skills(SKILLS_DIR)
    if not skills:
        print("  ❌ 没有可用的 skill")
        sys.exit(1)
    print(f"  共加载 {len(skills)} 个 skill")

    # 2. 获取真实 LLM
    print("\n[Step 2] 初始化真实 LLM...")
    try:
        llm = get_llm(purpose="generator")
        # 发一条简单的测试消息验证连通性
        test_resp = llm.invoke([("human", "回复 OK 表示连通")])
        test_text = test_resp.content if hasattr(test_resp, "content") else str(test_resp)
        print(f"  ✅ LLM 连通成功: {test_text.strip()[:50]}")
    except Exception as e:
        print(f"  ❌ LLM 初始化失败: {e}")
        print(f"    请确认 .env 文件中配置了 LLM_MODEL / LLM_BASE_URL / LLM_API_KEY")
        sys.exit(1)

    # 3. 测试多组路由场景
    test_cases = [
        ("数组+哈希表", "easy", "基础数组题"),
        ("动态规划", "medium", "经典 DP"),
        ("背包问题", "hard", "背包 DP，应该是 DP skill"),
        ("二叉树遍历", "medium", "没有明确匹配的 skill，看 LLM 怎么兜底"),
    ]

    for topic, difficulty, desc in test_cases:
        print(f"\n{'─' * 50}")
        print(f"  场景: {desc}")
        print(f"  输入: topic='{topic}', difficulty='{difficulty}'")
        print(f"{'─' * 50}")

        # 3a. LLM 路由
        route_result = llm_route(skills, topic, difficulty, llm)
        skill_name = route_result.get("skill_name", "")
        routed_topic = route_result.get("topic", topic)
        routed_difficulty = route_result.get("difficulty", difficulty)

        # 3b. 校验路由结果
        matched_skill = None
        for s in skills:
            if s.name == skill_name:
                matched_skill = s
                break

        if not matched_skill:
            print(f"  ❌ 路由结果 '{skill_name}' 不是有效的 skill 名称")
            print(f"     可用: {[s.name for s in skills]}")
            continue

        print(f"  ✅ 路由成功: {matched_skill.name}")

        # 3c. 用真实 LLM 调用选中的 skill 出题
        print(f"\n  [Step 3] 用 {matched_skill.name} 出题中（调用真实 LLM）...")
        print(f"  参数: topic={routed_topic}, difficulty={routed_difficulty}")
        try:
            result = matched_skill.invoke(llm.invoke, topic=routed_topic, difficulty=routed_difficulty)
            print(f"  ✅ 出题成功!")
            print(f"     标题: {result.get('title', '?')}")
            print(f"     难度: {result.get('difficulty', '?')}")
            print(f"     知识点: {result.get('topic', '?')}")
            print(f"     签名: {result.get('function_signature', '?')}")
            print(f"     最优解: {len(result.get('optimal_solution', ''))} 字符")

            # DP skill 特有的字段
            if "dp_type" in result:
                print(f"     DP 类型: {result.get('dp_type')}")
                print(f"     状态定义: {result.get('state_definition')}")
                print(f"     转移方程: {result.get('transition_formula')}")

        except Exception as e:
            print(f"  ❌ 出题失败: {e}")
            continue

    print(f"\n{'=' * 60}")
    print(f"  POC 完成")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()