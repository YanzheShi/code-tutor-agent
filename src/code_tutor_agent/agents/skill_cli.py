"""skill-engine CLI 逃生舱：subprocess 执行 skill 并收结果。

踩坑备忘（已规避）：
- Windows GBK 解码崩溃：Popen 必须 encoding="utf-8", errors="replace"，
  out/err 加 or "" 防御。
- skill 扫描依赖 cwd：spawn 时 cwd 必须指向 skill-engine 根目录。
- 中文 argv 乱码：PowerShell 直接塞中文到 argv 会乱码（环境编码问题，非 bug）。
  故一律用 list 形式传参（不走 shell 字符串拼接）；topic 优先用英文键，
  必要时改用 --args-file 临时文件/环境变量传参彻底绕开 argv 编码。

Phase 4（DP-5 延伸）：``run_skill_cli`` 返回结构与 import 主通道共用的
``SkillResult``（skills/result.py）**同形**，使上层（tools.py 的 mode 路由）
不感知通道差异，统一以 ``.ok`` / ``.output`` / ``.error`` 消费。
解析逻辑复用共享模块 skills/parser.parse_problem_markdown（与 adapter 同一份）。
"""

from __future__ import annotations

import json
import logging
import subprocess

from code_tutor_agent.config import (
    get_skill_engine_dir,
    get_skill_engine_cli_timeout,
    SKILL_ENGINE_CLI_ALLOWLIST,
)
# DP-5：解析逻辑抽到共享模块 skills/parser.py，import 主通道与 CLI 逃生舱共用。
# 此处 re-export 保持 `from skill_cli import parse_problem_markdown` 的历史导入不破。
from code_tutor_agent.skills.parser import (  # noqa: F401
    parse_problem_markdown,
    _strip_fences,
)
from code_tutor_agent.skills.result import SkillResult

logger = logging.getLogger(__name__)


def run_skill_cli(
    skill_name: str,
    args: dict,
    *,
    use_llm: bool = True,
    timeout: int | None = None,
) -> SkillResult:
    """执行一个 skill-engine skill，返回结构化结果（SkillResult 同形）。

    Args:
        skill_name: skill 名，必须在 SKILL_ENGINE_CLI_ALLOWLIST 内。
        args: skill 占位符字典（如 {"topic": "数组", "difficulty": "easy"}）。
        use_llm: 是否带 --llm（走 LLM 生成）；False 走 dry-run 模板。
        timeout: 子进程超时（秒），默认读配置。

    Returns:
        SkillResult：ok 表示成败；output 为主文本（markdown）；
        meta 携带 exit_code / stderr / channel（=“cli”）等逃生舱元信息；
        error 在 ok=False 时填原因。任何异常（超时/命令缺失）均归一为
        ok=False，**不向外抛**。
    """
    if skill_name not in SKILL_ENGINE_CLI_ALLOWLIST:
        return SkillResult(
            skill_name=skill_name,
            ok=False,
            error=f"skill '{skill_name}' 不在白名单 {sorted(SKILL_ENGINE_CLI_ALLOWLIST)}",
        )

    timeout = timeout or get_skill_engine_cli_timeout()
    # -a 参数序列化：
    #  - 含 $ARGUMENTS（整段题面，可能含逗号/换行）→ 直接作为整段，注入 skill 的 $ARGUMENTS
    #  - 否则按 skill 约定形如 topic=数组,difficulty=easy
    if "$ARGUMENTS" in args:
        args_str = str(args["$ARGUMENTS"])
    else:
        args_str = ",".join(f"{k}={v}" for k, v in args.items())
    cmd = ["skill-engine", "run", skill_name, "-a", args_str]
    if use_llm:
        cmd.append("--llm")

    try:
        proc = subprocess.run(
            cmd,
            cwd=get_skill_engine_dir(),          # 关键：否则扫不到 skills/
            capture_output=True,
            encoding="utf-8", errors="replace",  # 关键：防 Windows GBK 崩
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return SkillResult(
            skill_name=skill_name, ok=False,
            error=f"超时（>{timeout}s）",
        )
    except FileNotFoundError:
        return SkillResult(
            skill_name=skill_name, ok=False,
            error="skill-engine 命令未找到（SKILL_ENGINE_DIR 配错？）",
        )

    out = proc.stdout or ""
    err = proc.stderr or ""
    ok = proc.returncode == 0 and bool(out.strip())
    return SkillResult(
        skill_name=skill_name,
        ok=ok,
        output=out,
        meta={"exit_code": proc.returncode, "stderr": err, "channel": "cli"},
        error=None if ok else (err.strip() or f"exit_code={proc.returncode}"),
    )


def generate_problem_via_skill_sync(topic: str, difficulty: str) -> str:
    """Synchronous core: 通过 CLI 逃生舱出题，返回 JSON 字符串。

    复用共享 parser（DP-5）；消费 SkillResult 统一接口（.ok/.output/.error）。
    """
    r = run_skill_cli("cta-generate-problem", {"topic": topic, "difficulty": difficulty})
    if not r.ok:
        return json.dumps({"error": f"CLI 出题失败: {r.error}"}, ensure_ascii=False)
    parsed = parse_problem_markdown(r.output)
    if parsed is None:
        return json.dumps({"error": "CLI 出题成功但契约解析失败"}, ensure_ascii=False)
    return json.dumps(parsed, ensure_ascii=False)


def generate_detailed_solution_via_skill_sync(topic_description: str) -> str:
    """Synchronous core: 通过 CLI 逃生舱生成详细题解，返回 Markdown 文本字符串。

    ``topic_description`` 是整段题面，经 ``$ARGUMENTS`` 注入 skill。
    复用 SkillResult 统一接口（.ok/.output/.error）。
    """
    r = run_skill_cli("cta-generate-solution", {"$ARGUMENTS": topic_description})
    if not r.ok:
        return json.dumps(
            {"error": f"CLI 生成详细题解失败: {r.error}"}, ensure_ascii=False
        )
    return r.output.strip()
