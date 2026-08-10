"""出题 Agent（Problem Agent）。

把「出题」这一子任务收敛成一个独立 Agent，命名与 agents/agent_dialog.py、
agents/agent_judge.py 保持一致：本仓把「用 LLM 结构化输出完成特定子任务 +
自带结果模型 / 校验」的模块都叫 Agent。

出题 Agent 统合两类通道：

* **主通道（LLM）** ``generate_problem``：沿用 agent_dialog / agent_judge 的
  ``with_structured_output(Problem)`` 范式，直接产出 ``Problem`` 对象，并自带
  ``verify_problem`` 自校验与 ``max_tokens`` 限流（修复 Bug7 超时截断）。
* **兜底（静态题库）** ``store/static_pool``：LLM 多次重试仍失败时，回退到
  本仓自带题库（自家数据，非外部依赖）。

出题**不依赖任何外部工具**：核心能力的安全网必须是自身
可控的代码。

对外暴露统一入口 ``ProblemAgent.generate()``，按 「LLM → 静态兜底」降级，
命中通道随结果一并返回。

相关测试用例见 tests/test_problem_agent.py。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from langchain_core.prompts import ChatPromptTemplate

from code_tutor_agent.config import get_llm
from code_tutor_agent.models.problem import Problem
from code_tutor_agent.prompts.generate_problem import (
    GENERATE_PROBLEM_SYSTEM,
    GENERATE_PROBLEM_USER,
)

logger = logging.getLogger(__name__)


class ProblemChannel(str, Enum):
    """出题通道标识（设计 docs/generation-subagent-design.md §7）。"""

    LLM = "llm"                   # LLM 原创生成（主通道）
    LEETCODE_IMPORT = "leetcode_import"   # 用户贴 URL 导入成功
    LEETCODE_PULL = "leetcode_pull"       # LLM 失败后按主题拉 LeetCode 题
    DB_UNAC = "db_unac"                   # 历史未 AC 题
    STATIC = "static"             # 静态题库兜底

    def __str__(self) -> str:  # 让日志/测试打印出裸值而非 "ProblemChannel.LLM"
        return self.value


@dataclass
class GenerationOutcome:
    """一次出题尝试的结果：题目 + 实际命中通道 + 失败原因。"""

    problem: Optional[Problem]
    channel: ProblemChannel
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.problem is not None


# ──────────────────────────────────────────────
#  主通道：LLM 结构化出题
# ──────────────────────────────────────────────

def _extract_code(solution_text: str) -> str:
    """Extract Python code from LLM response, stripping markdown fences."""
    if match := re.search(r"```(?:python)?\s*\n(.*?)```", solution_text, re.DOTALL):
        return match.group(1).strip()
    return solution_text.strip()


# 仅树/图/链表题才允许的结构体类名；其它题型若在 starter_code 里冒出这些定义
# （LLM 套用 LeetCode 通用模板所致），出题时一律剥离，避免给用户展示无关模板。
_STRUCT_TOPIC_KEYWORDS = ("tree", "二叉树", "graph", "图", "linked", "链表")
_STRUCT_CLASS_NAMES = ("TreeNode", "ListNode", "GraphNode")


def _strip_unrelated_structs(starter_code: str, topic: str) -> str:
    """若题目不属于树/图/链表题型，移除 starter_code 中多余的 TreeNode/ListNode/GraphNode 定义。"""
    t = (topic or "").lower()
    if any(kw in t for kw in _STRUCT_TOPIC_KEYWORDS):
        return starter_code
    cleaned = starter_code
    for cls in _STRUCT_CLASS_NAMES:
        pat = re.compile(
            r"(?:^[ \t]*#.*\n)*^[ \t]*class\s+" + cls + r"\b[^\n]*\n(?:[ \t]+[^\n]*\n)*",
            re.MULTILINE,
        )
        cleaned = pat.sub("", cleaned)
    cleaned = cleaned.strip()
    return cleaned + ("\n" if cleaned else "")


def _is_stub_solution(code: str) -> bool:
    """判断代码是否只是占位桩（剥离空行/注释/定义骨架/pass/.../裸 return 后无真实逻辑）。

    用于拦截 ``class Solution:\\n    def solution(self):\\n        pass`` 这类
    「结构化合法但完全没内容」的空答案，避免被误判为合格题解。

    类定义、方法/函数定义（含其 ``->`` 返回标注）只是骨架，不算真实逻辑；
    一旦出现赋值/循环/条件/带返回值的 return 等真实语句，即判定为非桩。
    """
    for line in code.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s in ("pass", "...", "pass  # placeholder"):
            continue
        # 裸 return（无返回值）不算真实逻辑
        if re.fullmatch(r"return\s*(#.*)?", s):
            continue
        # 类 / 函数 / 方法定义骨架不算真实逻辑（允许返回标注 -> T）
        if re.fullmatch(r"(async\s+)?(class|def)\s+\w+(\s*\([^)]*\))?(\s*->\s*[\w\[\], ]*)?\s*:\s*", s):
            continue
        return False  # 出现任何真实语句 → 非桩
    return True


def verify_problem(problem_dict: dict) -> bool:
    """校验题目内容完整性 + optimal_solution 可编译、描述无思维链泄漏、starter_code 有效。

    会**原地**修正 problem_dict（去掉 ```python 围栏、必要时从 optimal_solution
    推导 starter_code / function_signature）。不跑测试用例（出题阶段用例尚未生成）。

    除代码层校验外，还校验内容已真正填写：标题/描述/示例/约束非空，且 optimal_solution
    不是仅含 ``pass`` 的桩——否则视为不合格，触发重试而非直接返回空题。
    """
    logger.info("▶ verify_problem() — checking optimal_solution compiles")

    # 内容完整性：标题 / 描述 / 示例 / 约束 必须真填了
    title = (problem_dict.get("title") or "").strip()
    if not title:
        logger.warning("title is empty — rejecting")
        return False
    desc = (problem_dict.get("description") or "").strip()
    if len(desc) < 10:
        logger.warning("description too short/empty — rejecting")
        return False
    examples = problem_dict.get("examples") or []
    if not examples or not str(examples[0]).strip():
        logger.warning("examples empty — rejecting")
        return False
    constraints = problem_dict.get("constraints") or []
    if not constraints or not str(constraints[0]).strip():
        logger.warning("constraints empty — rejecting")
        return False

    optimal = _extract_code(problem_dict.get("optimal_solution", ""))
    # 写回围栏后的版本，避免 ```python 围栏被落库 / 传入判题 runner 导致编译失败
    problem_dict["optimal_solution"] = optimal
    if not optimal:
        logger.warning("No optimal_solution — cannot verify")
        return False
    if _is_stub_solution(optimal):
        logger.warning("optimal_solution is a stub (no real logic) — rejecting")
        return False
    brute = _extract_code(problem_dict.get("brute_solution", ""))
    problem_dict["brute_solution"] = brute
    if not brute:
        logger.warning("No brute_solution — rejecting")
        return False
    if _is_stub_solution(brute):
        logger.warning("brute_solution is a stub (no real logic) — rejecting")
        return False

    # 检查描述中是否有思考过程泄漏（desc 已在上方取过）
    # 注意：只拦截真正的思维链泄露，不要误伤正常描述用语。
    # "这道题"、"其实"、"但是"、"经典的" 等是正常描述常见词，不应拦截。
    cot_keywords = ["让我们", "再试一个", "再试", "一步一步", "我们先", "我们来试试"]
    if any(kw in desc for kw in cot_keywords):
        logger.warning("Description contains chain-of-thought — rejecting")
        return False

    try:
        compile(optimal, "<optimal_solution>", "exec")
        logger.info("✓ optimal_solution compiles OK")
    except SyntaxError as exc:
        logger.warning("optimal_solution syntax error: %s", exc)
        return False

    # P0-3: 验证 brute_solution 也能编译
    try:
        compile(brute, "<brute_solution>", "exec")
        logger.info("✓ brute_solution compiles OK")
    except SyntaxError as exc:
        logger.warning("brute_solution syntax error: %s", exc)
        return False

    # 先去掉 LLM 结构化输出里可能夹带的 ```python 代码围栏
    sc = problem_dict.get("starter_code", "") or ""
    if sc:
        sc = _extract_code(sc)
        sc = _strip_unrelated_structs(sc, problem_dict.get("topic", ""))
        problem_dict["starter_code"] = sc
    if not sc or "class Solution" not in sc or "def " not in sc:
        logger.info("starter_code is missing or invalid — deriving from optimal_solution")
        # 从 optimal_solution 提取方法签名
        method_match = re.search(
            r'class Solution:\s+def (\w+)\(self([^)]*)\)\s*(?:->\s*(\w+(?:\[.*?\])?))?',
            optimal,
        )
        if method_match:
            method_name = method_match.group(1)
            params = method_match.group(2).strip()
            ret_type = method_match.group(3)
            ret_anno = f" -> {ret_type}" if ret_type else ""
            # 生成正确的 starter_code：class + 方法签名 + pass
            sc_generated = f"class Solution:\n    def {method_name}(self, {params}){ret_anno}:\n        pass\n"
            problem_dict["starter_code"] = sc_generated
            logger.info("Generated starter_code from optimal_solution: %s(...)", method_name)

    # 始终从 optimal_solution 提取 function_signature，覆盖 LLM 生成的值。
    # LLM 经常把 ListNode.__init__(val=0, next=None) 当成函数签名提取出来，
    # 导致 function_signature 错误（如 val=0,next=None -> None），进而让 runner
    # 无法正确把数组参数转换为 ListNode/TreeNode 对象。
    _method_match = re.search(
        r'class Solution:\s+def (\w+)\(self([^)]*)\)\s*(?:->\s*(\w+(?:\[.*?\])?))?',
        optimal,
    )
    if _method_match:
        _method_name = _method_match.group(1)
        sig_parts = optimal.split("def " + _method_name, 1)
        if len(sig_parts) > 1:
            sig_line = sig_parts[1].split("\n")[0].strip()
            # 去掉 'self, ' 前缀
            sig_line = re.sub(r'^\(self,\s*', '(', sig_line)
            sig_line = re.sub(r'^\(self\)', '()', sig_line)
            # 去掉末尾的 :（函数定义行结尾）
            sig_line = sig_line.rstrip(":")
            # 去掉外层括号，保持统一格式："(nums: List[int]) -> int" → "nums: List[int] -> int"
            # parse_signature() 的 _PARAM_RE 无法处理外层括号，会把 ) 吞入最后一个参数类型名
            if '->' in sig_line:
                _params_part, _return_part = sig_line.split('->', 1)
                _params_part = _params_part.strip().strip('()').strip()
                sig_line = f"{_params_part} -> {_return_part.strip()}"
            else:
                sig_line = sig_line.strip().strip('()').strip()
            problem_dict["function_signature"] = sig_line
            logger.info("Overrode function_signature from optimal_solution: %s", sig_line[:100])
        else:
            logger.warning("Could not parse method from optimal_solution — keeping as-is")

    return True


def generate_problem(
    topic: str,
    difficulty: str,
    purpose: str = "problem",
    max_retries: int =2,
) -> Problem:
    """（主通道）调用 LLM 结构化生成一道题，返回 ``Problem`` 对象。

    范式与 agent_dialog / agent_judge 一致：``ChatPromptTemplate | llm.with_structured_output``。

    * ``max_tokens=8192``：需容纳含完整 ``optimal_solution`` 的 ``Problem`` 结构化输出
      （非平凡题的题解可轻松超过 4096 完成 token，导致被截断、自校验失败、被迫降级到
      skill-engine 慢通道）。8192 留足余量且仍远低于 16384 token 硬上限（Bug7 截断风险）。
    * ``temperature=0.7``：增加多样性，减少 AC 题目重复出题。
    * 全部尝试（含重试）均失败（problem 为 None）时抛 ``RuntimeError``，交由上层降级。
    """
    logger.info("▶ generate_problem() — topic=%s difficulty=%s", topic, difficulty)
    # 限制输出长度：8192 足以容纳含完整题解的 Problem 结构化输出，同时远低于 16384 硬上限
    # （避免 Bug7 截断）；原 4096 对非平凡题过小，会触发「length limit reached」导致自校验失败。
    # temperature 调至 0.7 增加多样性，减少 AC 题目重复出题。
    # 全部尝试（含重试）均失败（problem 为 None）时抛 RuntimeError，交由 ProblemAgent 降级到静态库。
    llm = get_llm(purpose=purpose)
    structured_llm = llm.with_structured_output(Problem)

    prompt = ChatPromptTemplate.from_messages([
        ("system", GENERATE_PROBLEM_SYSTEM),
        ("human", GENERATE_PROBLEM_USER),
    ])

    chain = prompt | structured_llm

    problem: Problem | None = None
    # 调用重试
    for attempt in range(max_retries):
        logger.info("LLM call attempt %d/%d …", attempt + 1, max_retries + 1)

        try:
            problem = chain.invoke({"topic": topic, "difficulty": difficulty})
        except Exception as exc:
            logger.warning("LLM structured output failed: %s", exc)
            continue

        problem_dict = problem.model_dump()

        # 校验题目的完整性, 给出的暴力解和最优解能否编译
        if verify_problem(problem_dict):
            # 日志：打印 LLM 出题完整内容，方便调试
            logger.info(
                "LLM generate_problem OK — title=%s | topic=%s | diff=%s | "
                "starter_code=%s | func_sig=%s",
                problem_dict.get("title", "")[:80],
                problem_dict.get("topic", "")[:20],
                problem_dict.get("difficulty", ""),
                repr(problem_dict.get("starter_code", "")[:150]),
                problem_dict.get("function_signature", "")[:80],
            )
            logger.debug("Full problem dict: %s", problem_dict)
            # verify_problem 修改了 problem_dict（如从 optimal_solution 推导 starter_code），
            # 返回修改后的 Problem 对象，确保调用方拿到的是修正后的数据
            return Problem.model_validate(problem_dict)

        logger.warning("Self-verification failed on attempt %d — retrying", attempt + 1)
        logger.debug("Rejected problem dict: %s", problem_dict)

    if problem is None:
        raise RuntimeError("all generation attempts failed (LLM output unusable)")
    # 所有尝试要么抛异常、要么校验失败 → 抛异常让上层（ProblemAgent）降级到静态库，
    # 而不是把一道不合格（如空题/桩解）的题目返回给用户。
    raise RuntimeError("all generation attempts failed self-verification")


# ──────────────────────────────────────────────
#  兜底归一：把扁平 dict 归一为 Problem（静态题库 / 外部产物共用）
# ──────────────────────────────────────────────

def _flat_to_problem(flat: dict) -> Problem:
    """把 skill 通道的扁平 dict 归一为 ``Problem``。

    skill 通道产物（adapter / CLI）通常只含 title / topic / difficulty /
    description / function_signature / starter_code / optimal_solution /
    test_cases，**缺少** ``examples`` / ``constraints`` 等必填项，这里按字段
    默认值补齐（列表类补空列表），避免 pydantic 校验失败。
    """
    fields = Problem.model_fields
    data = {k: v for k, v in flat.items() if k in fields}
    for name, fld in fields.items():
        if name in data:
            continue
        if fld.default_factory is not None:
            data[name] = fld.default_factory()
        elif "list" in str(fld.annotation).lower():
            data[name] = []
        else:
            data[name] = fld.default if fld.default is not None else None
    return Problem(**data)


def _get_solution_llm():
    """详细题解专用 LLM（导师口吻，简单直出）。"""
    return get_llm(purpose="tutor", temperature=0.4)


_DETAILED_SOLUTION_PROMPT = (
    "你是算法导师。用户希望为下面这道题获得一份简洁、可教学的详细题解。\n"
    "请用中文输出，包含：解题思路（分步骤）、算法设计、时间与空间复杂度。\n"
    "代码用 ```python 围栏包裹。不要重复题目原文。\n\n"
    "题目描述：\n{description}\n\n请生成题解："
)


def generate_detailed_solution(problem_description: str) -> Optional[str]:
    """生成详细题解 markdown（导师 LLM 直出）；失败返回 ``None``。"""
    try:
        llm = _get_solution_llm()
        msg = _DETAILED_SOLUTION_PROMPT.format(description=problem_description)
        resp = llm.invoke(msg)
        text = getattr(resp, "content", None) or str(resp)
        return text.strip() or None
    except Exception as exc:
        logger.warning("详细题解生成失败: %s", exc)
        return None


# ──────────────────────────────────────────────
#  统一入口：ProblemAgent
# ──────────────────────────────────────────────

class ProblemAgent:
    """出题 Agent：统一入口，按降级链产出题目并报告命中通道。

    .. deprecated::
        generation/ 包（ProblemGenerationAgent）接管出题后已无活调用方
        （2026-08-10 审计确认，仅剩模块 docstring 示例引用）。请勿在新代码使用；
        待确认无外部依赖后整体删除。模块内 generate_problem / verify_problem 仍活跃
        （LlmGateway 底层），不在废弃范围。

    用法::

        agent = ProblemAgent(topic="数组", difficulty="easy")
        outcome = agent.generate()
        if outcome.ok:
            problem = outcome.problem          # Problem
            print(outcome.channel)             # ProblemChannel.LLM / ADAPTER / CLI / STATIC
    """

    def __init__(self, topic: str, difficulty: str):
        self.topic = topic
        self.difficulty = difficulty

    # 降级顺序：原生 LLM（自带重试 + 自校验）→ 静态题库兜底
    def generate(self) -> GenerationOutcome:
        """出题并尝试逐级降级，返回 ``GenerationOutcome``。

        出题是核心能力，安全网必须是自身可控代码：先走原生 LLM（``generate_problem``
        内部含 max_retries 重试 + ``verify_problem`` 自校验），全部失败再回退本仓
        静态题库（自家数据，非外部依赖）。不再依赖 skill-engine 等外部工具。
        """
        # 1) 主通道：原生 LLM 结构化出题
        try:
            return GenerationOutcome(
                generate_problem(self.topic, self.difficulty),
                ProblemChannel.LLM,
            )
        except Exception as exc:
            logger.warning("LLM 出题全部尝试失败，回退静态题库: %s", exc)

        # 2) 静态题库兜底（自家数据，非外部依赖）
        try:
            from code_tutor_agent.store.static_pool import get_static_problem

            flat = get_static_problem(topic=self.topic, difficulty=self.difficulty)
            if flat is None:
                flat = get_static_problem()
            if flat is not None:
                return GenerationOutcome(_flat_to_problem(flat), ProblemChannel.STATIC)
        except Exception as exc:
            logger.warning("静态题库兜底失败: %s", exc)

        return GenerationOutcome(None, ProblemChannel.STATIC, error="所有出题通道均失败")
