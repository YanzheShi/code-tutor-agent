"""Agent 判题 Agent — LLM 驱动的代码分析和温暖反馈生成。

通过 Judge0 对测试用例执行用户代码后，
本模块让 LLM 解读原始结果，生成有教育意义的鼓励性反馈和具体修复建议。

流程：
    1. Judge0 原始结果输入（每个用例的 pass/fail/runtime）
    2. LLM 读取代码 + 结果 + 题目描述
    3. LLM 生成结构化分析（含温暖反馈）
    4. 如果代码失败 → 修复建议
    5. 如果代码通过 → 优化建议
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from code_tutor_agent.config import get_llm
from code_tutor_agent.sandbox.runner import RunnerResult

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  结构化输出模型
# ──────────────────────────────────────────────


class JudgeAnalysis(BaseModel):
    """LLM structured output: analysis of one judge cycle."""

    verdict: str = Field(
        description="整体 verdict: AC (全部通过) / WA (答案错误) / RE (运行时错误) / TLE (超时)",
    )
    warm_feedback: str = Field(
        description="给用户的温暖、鼓励性反馈。AC 时祝贺，WA 时安慰+鼓励。中文，2-3句话",
    )
    repair_suggestion: str = Field(
        default="",
        description="如果代码有误，给出具体修复建议：指出错误位置、原因、建议修改方向。"
                    "AC 时填空字符串或给出优化建议",
    )
    should_retry: bool = Field(
        default=False,
        description="用户是否需要修改代码后重新提交？True=需要继续改，False=已达标",
    )


# ──────────────────────────────────────────────
#  Prompt 模板
# ──────────────────────────────────────────────

JUDGE_ANALYSIS_SYSTEM = """你是 AI 编程导师，负责分析学生提交的代码和判题结果。

## 核心原则

1. **温暖第一** — 即使代码错了，也要先肯定学生的努力（"已经接近了！"、"思路是对的"）
2. **具体有用** — 指出具体哪个用例错了、期望输出 vs 实际输出、为什么错
3. **面试导向** — 从面试官角度分析：这道题考察的核心算法思维是什么？学生的解法在面试中能拿多少分？
4. **自由引导** — 按学生需要提供提示，必要时可直接给出代码示例，像真人私教一样灵活

## 分析步骤

1. **看题目**：理解题目要求和约束条件
2. **看学生代码**：理解学生的算法思路
3. **看判题结果**：
   - 每个用例：期望输出是什么、学生实际输出是什么
   - 差异能反映什么逻辑错误
4. **综合判断**：
   - 全部通过 → AC，分析时间/空间复杂度是否最优
   - 部分通过 → WA，找出具体逻辑漏洞
   - 全部超时 → TLE，分析算法复杂度问题
   - 全部报错 → RE/CE，分析语法或运行时错误

## AC 时的输出要求

即使 AC 了也要做三件事：
1. 祝贺学生（简短）
2. **复杂度分析**：评估学生代码的时空复杂度，告诉学生是否达到最优
3. **优化建议**：如果还有优化空间（如暴力解可以改哈希表），给出方向提示

## WA/TLE/RE 时的输出要求

1. **先肯定** — "代码结构很好，只是一个小细节没处理好"
2. **定位问题** — 哪个用例失败了？期望输出 vs 实际输出各是什么？
3. **分析原因** — 说明背后的逻辑错误（如"边界条件没处理空数组"、"循环条件少了一个等号"）
4. **修复方向** — 给提示，必要时可直接给出代码示例（按学生请求）
5. **面试提示** — 如果是面试题，面试官会重点考察哪一点？

## 输出格式

返回 JSON:
- 注意：最终 verdict 由执行引擎的客观结果决定（prompt 末尾会给出【权威判题结果】），
  你必须以其为准撰写反馈，禁止自行改判，也不要臆造任何失败用例的「实际输出」。
- verdict: "AC" / "WA" / "RE" / "TLE"
- warm_feedback: 2-3 句温暖反馈，中文，面试导向（AC 时含复杂度分析）
- repair_suggestion: 具体修复建议（AC 时给优化建议，WA/TLE 给出方向和提示）
- should_retry: 需要学生继续改吗？(AC=false, 其他=true)
"""

JUDGE_ANALYSIS_USER = """## 题目

**标题**: {title}
**难度**: {difficulty}
**知识点**: {topic}
**描述**: {description}

## 学生代码

```python
{code}
```

## 判题结果（共 {total} 个用例）

{results_text}

### 判题结果说明
- ✅ = 通过（学生输出与期望一致）
- ❌ = 未通过（学生输出与期望不一致）

**请特别注意未通过用例的期望输出 vs 实际输出差异**，这能帮你精准定位学生的逻辑漏洞。

分析时请思考：
1. 这道题在面试中主要考察什么算法思维？
2. 学生的解法是暴力解还是最优解？时空复杂度是多少？
3. 如果是 WA，学生的输出和期望输出的差异说明了什么逻辑错误？
4. 如果是 TLE，学生需要优化算法的哪个方面？

请分析学生的代码表现，给出温暖反馈和修复建议。"""


# ──────────────────────────────────────────────
#  核心函数
# ──────────────────────────────────────────────


def format_results_for_prompt(results: list[RunnerResult]) -> str:
    """Format RunnerResult list into a human-readable string for the LLM prompt.

    Args:
        results: Raw results from run_solution().

    Returns:
        Formatted string, one line per test case.

    注意：每个用例都展示「期望输出 / 实际输出」，即使是通过的用例也展示，
    避免 LLM 在看不到实际输出时臆造错误的「你的输出」。
    """
    lines = []
    for i, r in enumerate(results, 1):
        status_icon = "✅" if r.status == "Passed" else "❌"
        runtime = f"{r.runtime_ms:.1f}ms" if r.runtime_ms > 0 else ""
        memory = f"{r.memory_kb:.0f}KB" if r.memory_kb > 0 else ""
        extra = " | ".join(filter(None, [runtime, memory]))
        lines.append(f"  用例 #{i}: {status_icon} {r.status}  {extra}")
        exp = getattr(r, "expected_output", "") or ""
        act = getattr(r, "actual_output", "") or ""
        if exp or act:
            lines.append(f"          期望输出={exp!r} 实际输出={act!r}")
        elif r.status != "Passed" and r.detail:
            lines.append(f"          {r.detail[:200]}")
    return "\n".join(lines)


def _deterministic_verdict(results: list) -> str:
    """由执行引擎的客观结果归约出权威 verdict——绝不交给 LLM 主观判断。

    优先级：TLE > RE > WA > AC。「Skipped」= 无参考答案（空 expected），
    不参与判定，视为通过，避免误判 WA。
    """
    judged = [r for r in results if getattr(r, "status", "") != "Skipped"]
    if not judged:
        return "AC"
    if any(r.status == "TLE" for r in judged):
        return "TLE"
    if any(r.status == "Runtime Error" for r in judged):
        return "RE"
    if any(r.status == "Wrong Answer" for r in judged):
        return "WA"
    if all(r.status == "Passed" for r in judged):
        return "AC"
    return "WA"


def analyze_judge_results(
    code: str,
    title: str,
    difficulty: str,
    topic: str,
    description: str,
    results: list[RunnerResult],
    purpose: str = "judge",
    forced_verdict: str | None = None,
) -> JudgeAnalysis:
    """Ask the LLM to analyze raw judge results and produce warm feedback.

    Args:
        code: The user's submitted code.
        title: Problem title.
        difficulty: Problem difficulty.
        topic: Problem knowledge point.
        description: Problem description text.
        results: Raw RunnerResult list from run_solution().
        purpose: LLM model purpose.
        forced_verdict: 权威 verdict（来自执行引擎客观结果）。若提供，则最终
            返回的 ``verdict`` 与 ``should_retry`` 一律以它为准，LLM 只负责
            生成反馈文案，不得自行改判或臆造失败输出。

    Returns:
        JudgeAnalysis with verdict, feedback, and repair suggestion.
    """
    logger.info("▶ analyze_judge_results() — %d test cases", len(results))
    total = len(results)
    passed = sum(1 for r in results if r.status == "Passed")
    # 权威 verdict：永远以执行引擎客观结果为准，LLM 的主观判断不可信。
    authoritative = forced_verdict or _deterministic_verdict(results)
    results_text = format_results_for_prompt(results)

    user_prompt = JUDGE_ANALYSIS_USER.format(
        title=title,
        difficulty=difficulty,
        topic=topic,
        description=description[:3000] if description else "",
        code=code[:12000] if code else "",
        total=total,
        results_text=results_text,
    )
    # 把权威 verdict 显式注入，约束 LLM 只能据此生成反馈，不得自行改判/臆造输出。
    user_prompt += (
        f"\n\n【权威判题结果（来自执行引擎，必须以此为准，禁止质疑或自行改判）】："
        f"{authoritative}\n"
        f"请严格基于上述客观结果撰写反馈：若 verdict=AC，请直接祝贺并给复杂度/优化建议，"
        f"不要声称代码有误，也不要臆造任何失败用例的「实际输出」；若 verdict≠AC，"
        f"请结合上面每个用例的真实「期望输出/实际输出」定位问题。"
    )

    try:
        llm = get_llm(purpose=purpose, temperature=0.7)
        structured_llm = llm.with_structured_output(JudgeAnalysis)

        analysis: JudgeAnalysis = structured_llm.invoke([
            ("system", JUDGE_ANALYSIS_SYSTEM),
            ("human", user_prompt),
        ])
        # 最终 verdict 以客观执行结果为准，绝不被 LLM 覆盖。
        analysis.verdict = authoritative
        analysis.should_retry = (authoritative != "AC")
        logger.info(
            "analyze_judge_results() → verdict=%s (authoritative) retry=%s passed=%d/%d",
            analysis.verdict, analysis.should_retry, passed, total,
        )
        return analysis
    except Exception as exc:
        logger.warning("LLM judge analysis failed: %s — using fallback", exc)
        # 降级：verdict 仍以执行引擎客观结果为准，绝不误判。
        if authoritative == "AC":
            return JudgeAnalysis(
                verdict="AC",
                warm_feedback="恭喜！你的代码通过了所有测试用例！🎉",
                repair_suggestion="",
                should_retry=False,
            )
        else:
            fail_details = []
            for r in results:
                if r.status not in ("Passed", "Skipped"):
                    fail_details.append(f"用例 #{r.test_case_id}: {r.status} — {r.detail[:100]}")
            fail_text = "; ".join(fail_details[:3]) or "部分用例未通过"
            return JudgeAnalysis(
                verdict=authoritative,
                warm_feedback="你的代码已经跑起来了，但还有一些用例没通过。别灰心，看看具体哪里出了问题 👀",
                repair_suggestion=f"未通过的用例：{fail_text}",
                should_retry=True,
            )