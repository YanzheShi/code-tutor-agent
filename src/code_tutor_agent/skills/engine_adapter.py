"""skill-engine import 主通道适配层（进程内、确定性 bootstrap，绕过 Router）。

设计见 docs/skill-engine-adapter-design.md §6。核心：
- ``run_skill``：通用入口；任何错误归一为 ``SkillResult(ok=False)``，**不向外抛**。
- ``generate_problem``：出题 → 复用共享 parser → 返回**扁平 problem dict**
  （可直接喂 ``db.save_problem`` / generator.py 路径 C，与 CLI 逃生舱产物同形）。
- ``generate_detailed_solution``：详细题解 → 直接返回 markdown 文本（不结构化）。

真实 skill_engine API（Phase 0 已核对 2026-07-18）：
- ``skill_engine.routing.discovery.discover(roots=[...])`` → ``{name: SkillMeta}``
- ``skill_engine.routing.registry.Registry(index).load_skill(name)`` → ``Skill | None``
- ``skill_engine.models.MatchResult(skill, score, method, arguments)``
- ``skill_engine.execution.{executor.Executor, assembler.Assembler, runner.Runner}``
- ``Runner.run(match_result, llm=)`` 走档位 A 单次 LLM 调用，返回 dict（``output`` 主文本）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from code_tutor_agent.config import (
    get_llm,
    get_skill_engine_llm_alias,
    get_skill_engine_skills_root,
)
from code_tutor_agent.skills.parser import parse_problem_markdown

logger = logging.getLogger(__name__)


# ── 错误类型 ──
class SkillError(Exception):
    """skill 适配层错误基类。"""


class SkillConfigError(SkillError):
    """用户参数错 / skill 未找到 / LLM 不可用（可重试提示，4xx 语义）。"""


class SkillExecutionError(SkillError):
    """引擎内部执行 / 解析失败（告警 + 5xx 语义）。"""


# ── 结果结构（双通道共用，定义见 skills/result.py；re-export 保持兼容）──
from code_tutor_agent.skills.result import SkillResult  # noqa: F401


DEFAULT_PROBLEM_SKILL = "cta-generate-problem"
DEFAULT_DETAILED_SOLUTION_SKILL = "cta-generate-solution"
# DP-2：本次仅实现 problem + detailed-solution；generate_problem 预留 skill_name 形参，
# 为多题型（math/sci/eng/ai）留口子，本次不接。


def _bootstrap(
    skill_name: str,
    arguments: dict | None,
    llm_alias: str | None,
) -> str:
    """确定性 bootstrap：discover → Registry → MatchResult → Runner.run(llm=)。

    绕过 Router（不做 NLP 匹配），用固定 MatchResult 注入本系统 LLM，
    返回 skill 的 ``output`` 主文本。失败按语义抛 SkillConfigError / SkillExecutionError。
    """
    from skill_engine.routing.discovery import discover
    from skill_engine.routing.registry import Registry
    from skill_engine.models import MatchResult
    from skill_engine.execution.executor import Executor
    from skill_engine.execution.assembler import Assembler
    from skill_engine.execution.runner import Runner

    skills_root = get_skill_engine_skills_root()
    index = discover(roots=[skills_root])              # 绝对路径，不经 cwd
    if skill_name not in index:
        raise SkillConfigError(
            f"skill 未找到: {skill_name}（roots={skills_root}，"
            f"已发现={sorted(index.keys())}）"
        )
    registry = Registry(index)
    skill = registry.load_skill(skill_name)
    if skill is None:
        raise SkillConfigError(f"skill 加载失败: {skill_name}")

    alias = llm_alias or get_skill_engine_llm_alias()
    try:
        llm = get_llm(alias, temperature=0.7)          # 本系统 LLM 单一真源
    except Exception as exc:  # 缺 key / 别名错 → 配置类错误
        raise SkillConfigError(f"LLM 初始化失败（alias={alias}）: {exc}") from exc

    mr = MatchResult(
        skill=skill,
        score=1.0,
        method="exact",
        arguments=dict(arguments or {}),
    )
    executor = Executor(timeout=60, allow_all=True)
    assembler = Assembler(executor=executor, command_timeout=60)
    runner = Runner(assembler, executor)

    result = runner.run(mr, llm=llm)                   # 档位 A 单次 LLM 调用
    output = (result.get("output") or "").strip()
    # Runner 成功不置 top-level error；LLM 失败以占位文本返回，这里统一识别。
    if result.get("error"):
        raise SkillExecutionError(f"skill 返回错误: {result['error']}")
    if not output or output.startswith("[LLM 调用失败"):
        raise SkillExecutionError(f"skill 输出异常: {output[:200] or '空输出'}")
    return output


def run_skill(
    skill_name: str,
    *,
    arguments: dict[str, str] | None = None,
    llm_alias: str | None = None,
) -> SkillResult:
    """通用 skill 运行入口（import 通道，确定性）。

    成功返回 ``ok=True`` 与 markdown；任何错误归一为 ``ok=False``（error 字段），
    **不向外抛**（便于上层无脑降级）。
    """
    started = time.perf_counter()
    arg_keys = sorted((arguments or {}).keys())
    try:
        output = _bootstrap(skill_name, arguments, llm_alias)
    except SkillError as exc:
        logger.warning(
            "run_skill 失败 skill=%s keys=%s err=%s", skill_name, arg_keys, exc
        )
        return SkillResult(skill_name=skill_name, ok=False, error=str(exc))
    except Exception as exc:  # 兜底：未预期异常也不冒泡
        logger.warning(
            "run_skill 未预期异常 skill=%s keys=%s err=%s", skill_name, arg_keys, exc
        )
        return SkillResult(skill_name=skill_name, ok=False, error=str(exc))

    dur = time.perf_counter() - started
    logger.info(
        "run_skill ok skill=%s keys=%s dur=%.2fs len=%d",
        skill_name, arg_keys, dur, len(output),
    )
    return SkillResult(
        skill_name=skill_name,
        ok=True,
        output=output,
        meta={"duration_s": round(dur, 3), "channel": "adapter"},
    )


def generate_problem(
    topic: str,
    difficulty: str,
    *,
    skill_name: str = DEFAULT_PROBLEM_SKILL,
    llm_alias: str | None = None,
    max_retries: int = 1,
) -> dict:
    """出题：跑 skill → 共享 parser 解析 → 返回扁平 problem dict。

    返回的 dict 字段（title / topic / difficulty / description / starter_code /
    optimal_solution / test_cases）与 CLI 逃生舱产物同形，可直接喂 db.save_problem。

    失败时带原因重跑（最多 max_retries 次额外重试），耗尽抛 SkillExecutionError。

    注：设计文档 §6.2 早期草案写 ``-> ProblemMeta``，但 ProblemMeta 需 DB 分配的
    problem_id 且字段为 visible_test_cases；save_problem 实际吃扁平 dict，故此处
    返回扁平 dict，与 generator.py 路径 C 现状一致。
    """
    last_err: str | None = None
    for attempt in range(max_retries + 1):
        res = run_skill(
            skill_name,
            arguments={"topic": topic, "difficulty": difficulty},
            llm_alias=llm_alias,
        )
        if not res.ok:
            last_err = res.error
            logger.warning(
                "generate_problem 第 %d 次失败: %s", attempt + 1, res.error
            )
            continue
        parsed = parse_problem_markdown(res.output)
        if parsed is None:
            last_err = "契约解析失败（缺少 ## 分节）"
            logger.warning("generate_problem 第 %d 次解析失败", attempt + 1)
            continue
        if not parsed.get("title") or parsed.get("title") == "Untitled":
            last_err = "解析结果缺少有效 title"
            logger.warning("generate_problem 第 %d 次缺 title", attempt + 1)
            continue
        return parsed

    raise SkillExecutionError(
        f"出题失败（重试 {max_retries} 次耗尽）: {last_err or '未知原因'}"
    )


def generate_detailed_solution(
    problem_description: str,
    *,
    skill_name: str = DEFAULT_DETAILED_SOLUTION_SKILL,
    llm_alias: str | None = None,
) -> str:
    """详细题解：给定整题 description，返回 markdown 文本（不解析进结构）。

    ``problem_description`` 整段经 ``$ARGUMENTS`` 注入 skill。
    失败抛 SkillExecutionError（上层可捕获降级）。
    """
    res = run_skill(
        skill_name,
        arguments={"$ARGUMENTS": problem_description},
        llm_alias=llm_alias,
    )
    if not res.ok:
        raise SkillExecutionError(f"生成详细题解失败: {res.error}")
    return res.output.strip()
