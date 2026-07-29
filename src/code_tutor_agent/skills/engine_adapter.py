"""skill-engine import 主通道适配层（进程内、确定性 bootstrap，绕过 Router）。

设计见 docs/skill-engine-adapter-design.md §6。核心：
- ``run_skill``：通用入口；任何错误归一为 ``SkillResult(ok=False)``，**不向外抛**。
- ``generate_detailed_solution``：详细题解 → 直接返回 markdown 文本（不结构化）。

注：出题（generate_problem）已收口到 ``ProblemAgent``（原生 LLM + 静态兜底），
不再经由本适配层；本模块现仅服务「详细题解」这一仍走 skill-engine 的能力。

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
    get_skill_engine_purpose,
    get_skill_engine_skills_root,
)

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


DEFAULT_DETAILED_SOLUTION_SKILL = "cta-generate-solution"


def _bootstrap(
    skill_name: str,
    arguments: dict | None,
    purpose: str | None,
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

    p = purpose or get_skill_engine_purpose()
    try:
        llm = get_llm(purpose=p)          # 本系统 LLM 单一真源
    except Exception as exc:  # 缺 key / 用途错 → 配置类错误
        raise SkillConfigError(f"LLM 初始化失败（purpose={p}）: {exc}") from exc

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
    purpose: str | None = None,
) -> SkillResult:
    """通用 skill 运行入口（import 通道，确定性）。

    成功返回 ``ok=True`` 与 markdown；任何错误归一为 ``ok=False``（error 字段），
    **不向外抛**（便于上层无脑降级）。
    """
    started = time.perf_counter()
    arg_keys = sorted((arguments or {}).keys())
    try:
        output = _bootstrap(skill_name, arguments, purpose)
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


def generate_detailed_solution(
    problem_description: str,
    *,
    skill_name: str = DEFAULT_DETAILED_SOLUTION_SKILL,
    purpose: str | None = None,
) -> str:
    """详细题解：给定整题 description，返回 markdown 文本（不解析进结构）。

    ``problem_description`` 整段经 ``$ARGUMENTS`` 注入 skill。
    失败抛 SkillExecutionError（上层可捕获降级）。
    """
    res = run_skill(
        skill_name,
        arguments={"$ARGUMENTS": problem_description},
        purpose=purpose,
    )
    if not res.ok:
        raise SkillExecutionError(f"生成详细题解失败: {res.error}")
    return res.output.strip()
