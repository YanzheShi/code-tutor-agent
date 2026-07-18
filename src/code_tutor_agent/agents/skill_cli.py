"""skill-engine CLI 逃生舱：subprocess 执行 skill 并收结果。

踩坑备忘（已规避）：
- Windows GBK 解码崩溃：Popen 必须 encoding="utf-8", errors="replace"，
  out/err 加 or "" 防御。
- skill 扫描依赖 cwd：spawn 时 cwd 必须指向 skill-engine 根目录。
- 中文 argv 乱码：PowerShell 直接塞中文到 argv 会乱码（环境编码问题，非 bug）。
  故一律用 list 形式传参（不走 shell 字符串拼接）；topic 优先用英文键，
  必要时改用 --args-file 临时文件/环境变量传参彻底绕开 argv 编码。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess

from code_tutor_agent.config import (
    get_skill_engine_dir,
    get_skill_engine_cli_timeout,
    SKILL_ENGINE_CLI_ALLOWLIST,
)
from code_tutor_agent.leetcode.leetcode_fetcher import _parse_examples_to_test_cases

logger = logging.getLogger(__name__)


def run_skill_cli(
    skill_name: str,
    args: dict,
    *,
    use_llm: bool = True,
    timeout: int | None = None,
) -> dict:
    """执行一个 skill-engine skill，返回结构化结果。

    Args:
        skill_name: skill 名，必须在 SKILL_ENGINE_CLI_ALLOWLIST 内。
        args: skill 占位符字典（如 {"topic": "数组", "difficulty": "easy"}）。
        use_llm: 是否带 --llm（走 LLM 生成）；False 走 dry-run 模板。
        timeout: 子进程超时（秒），默认读配置。

    Returns:
        {
          "ok": bool,
          "exit_code": int,
          "stdout": str,
          "stderr": str,
          "skill_name": str,
          "error": str | None,   # ok=False 时填原因
        }
    """
    if skill_name not in SKILL_ENGINE_CLI_ALLOWLIST:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "",
                "skill_name": skill_name,
                "error": f"skill '{skill_name}' 不在白名单 {sorted(SKILL_ENGINE_CLI_ALLOWLIST)}"}

    timeout = timeout or get_skill_engine_cli_timeout()
    # -a 参数序列化：skill 约定形如 topic=数组,difficulty=easy
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
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "",
                "skill_name": skill_name, "error": f"超时（>{timeout}s）"}
    except FileNotFoundError:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "",
                "skill_name": skill_name, "error": "skill-engine 命令未找到（SKILL_ENGINE_DIR 配错？）"}

    out = proc.stdout or ""
    err = proc.stderr or ""
    ok = proc.returncode == 0 and bool(out.strip())
    return {
        "ok": ok,
        "exit_code": proc.returncode,
        "stdout": out,
        "stderr": err,
        "skill_name": skill_name,
        "error": None if ok else (err.strip() or f"exit_code={proc.returncode}"),
    }


# cta-generate-problem 输出契约节（## Title / Topic / Difficulty / Description /
# Examples / Constraints / StarterCode / BruteSolution / OptimalSolution）
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _strip_fences(s: str) -> str:
    """去掉 Markdown 代码围栏（```lang ... ```），无围栏则原样 strip。"""
    m = re.search(r"```[a-zA-Z]*\n?(.*?)```", s, re.DOTALL)
    return m.group(1).strip() if m else s.strip()


def parse_problem_markdown(stdout: str) -> dict | None:
    """从 skill 输出里抠出契约节 → 扁平 dict。

    返回 None 表示解析不到任何契约节（stdout 异常）。
    字段映射严格对应 ProblemMeta（见 schemas/state.py）。
    """
    if not stdout or "##" not in stdout:
        return None
    # 取最后一个 "====" 之后的 body（屏蔽 run 命令自身的前缀日志）
    body = stdout.split("====")[-1] if "====" in stdout else stdout
    parts = _SECTION_RE.split(body)
    # parts: ['', 'Title', '...', 'Topic', '...', ...]
    if len(parts) < 3:
        return None
    sections: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        sections[parts[i].strip().lower()] = parts[i + 1].strip()

    examples_raw = sections.get("examples", "")
    sample_tcs = _parse_examples_to_test_cases(examples_raw, "")  # 复用现有解析

    return {
        "title": sections.get("title", "Untitled"),
        "topic": sections.get("topic", "数组"),
        "difficulty": (sections.get("difficulty", "easy") or "easy").lower(),
        "description": sections.get("description", ""),
        "starter_code": _strip_fences(sections.get("startercode", "")),
        "optimal_solution": _strip_fences(
            sections.get("optimalsolution", "") or sections.get("brutesolution", "")
        ),
        "test_cases": sample_tcs,
    }


def generate_problem_via_skill_sync(topic: str, difficulty: str) -> str:
    """Synchronous core: 通过 CLI 逃生舱出题，返回 JSON 字符串。"""
    r = run_skill_cli("cta-generate-problem", {"topic": topic, "difficulty": difficulty})
    if not r["ok"]:
        return json.dumps({"error": f"CLI 出题失败: {r['error']}"}, ensure_ascii=False)
    parsed = parse_problem_markdown(r["stdout"])
    if parsed is None:
        return json.dumps({"error": "CLI 出题成功但契约解析失败"}, ensure_ascii=False)
    return json.dumps(parsed, ensure_ascii=False)
