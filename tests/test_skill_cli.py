"""skill-engine CLI 逃生舱测试：runner 封装 + Markdown 契约解析。

全程 mock ``subprocess.run``，不依赖真实 skill-engine 环境，离线全绿。

Phase 4（DP-5 延伸）：``run_skill_cli`` 现返回 ``SkillResult``（与 import
主通道同形），故断言从字典访问改为 ``.ok`` / ``.output`` / ``.error`` 等属性。
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from code_tutor_agent.agents import skill_cli
from code_tutor_agent.agents.skill_cli import run_skill_cli, parse_problem_markdown
from code_tutor_agent.skills.result import SkillResult

# 契约 Markdown 样例（带 run 命令前缀日志 + ==== 分隔），用于 run_skill_cli / parse_problem_markdown 测试
CONTRACT_MD = """\
[skill-engine] loading skills from ./skills ...
==== result ====
## Title
Move Zeroes
## Topic
数组
## Difficulty
easy
## Description
将数组中所有 0 移动到末尾，保持非零元素相对顺序。
## Examples
Example 1:
Input: nums = [0,1,0,3,2]
Output: [1,3,2,0,0]
## Constraints
1 <= nums.length <= 10^4
## StarterCode
```python
class Solution:
    def moveZeroes(self, nums: list[int]) -> None:
        pass
```
## BruteSolution
```python
class Solution:
    def moveZeroes(self, nums):
        n = len(nums)
        for i in range(n):
            if nums[i] == 0:
                nums.remove(0)
                nums.append(0)
```
## OptimalSolution
```python
class Solution:
    def moveZeroes(self, nums):
        slow = 0
        for fast in range(len(nums)):
            if nums[fast] != 0:
                nums[slow], nums[fast] = nums[fast], nums[slow]
                slow += 1
```
"""


def _fake_proc(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    p = MagicMock()
    p.stdout = stdout
    p.stderr = stderr
    p.returncode = returncode
    return p


# ──────────────────────────────────────────────
#  run_skill_cli
# ──────────────────────────────────────────────


def test_run_skill_cli_rejects_unlisted_skill():
    """白名单之外的 skill 名直接拦截，不 spawn 进程。"""
    with patch("code_tutor_agent.agents.skill_cli.subprocess.run") as run_mock:
        r = run_skill_cli("evil-skill", {"topic": "x"})
    run_mock.assert_not_called()
    assert r.ok is False
    assert "白名单" in r.error
    assert r.skill_name == "evil-skill"


def test_run_skill_cli_success_parses_stdout():
    """正常执行：返回 ok=True 并透传 output/exit_code。"""
    with patch(
        "code_tutor_agent.agents.skill_cli.subprocess.run",
        return_value=_fake_proc(CONTRACT_MD, returncode=0),
    ):
        r = run_skill_cli("cta-generate-solution", {"topic": "数组", "difficulty": "easy"})
    assert r.ok is True
    assert r.meta["exit_code"] == 0
    assert "Move Zeroes" in r.output
    assert r.error is None


def test_run_skill_cli_empty_stdout_is_failure():
    """非零 exit 或空 stdout 都算失败。"""
    with patch(
        "code_tutor_agent.agents.skill_cli.subprocess.run",
        return_value=_fake_proc("", returncode=1, stderr="boom"),
    ):
        r = run_skill_cli("cta-generate-solution", {"topic": "数组"})
    assert r.ok is False
    assert r.error == "boom"


def test_run_skill_cli_command_not_found():
    """skill-engine 命令不存在 → 不抛异常，转成 ok=False。"""
    with patch(
        "code_tutor_agent.agents.skill_cli.subprocess.run",
        side_effect=FileNotFoundError(),
    ):
        r = run_skill_cli("cta-generate-solution", {"topic": "数组"})
    assert r.ok is False
    assert "未找到" in r.error


def test_run_skill_cli_timeout():
    """子进程超时 → 不抛异常，转成 ok=False。"""
    with patch(
        "code_tutor_agent.agents.skill_cli.subprocess.run",
        side_effect=subprocess.TimeoutExpired("skill-engine", 60),
    ):
        r = run_skill_cli("cta-generate-solution", {"topic": "数组"}, timeout=60)
    assert r.ok is False
    assert "超时" in r.error


def test_run_skill_cli_builds_cmd_with_args_and_llm_flag():
    """命令拼装：list 形式传参 + 默认带 --llm。"""
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        captured["encoding"] = kwargs.get("encoding")
        return _fake_proc(CONTRACT_MD)

    with patch("code_tutor_agent.agents.skill_cli.subprocess.run", side_effect=_fake_run):
        run_skill_cli("cta-generate-solution", {"topic": "数组", "difficulty": "easy"})
    assert captured["cmd"][:3] == ["skill-engine", "run", "cta-generate-solution"]
    assert captured["cmd"][3] == "-a"
    assert "topic=数组,difficulty=easy" in captured["cmd"][4]
    assert "--llm" in captured["cmd"]
    assert captured["encoding"] == "utf-8"


# ──────────────────────────────────────────────
#  parse_problem_markdown
# ──────────────────────────────────────────────


def test_parse_problem_markdown_maps_contract():
    """契约 Markdown → 扁平 dict，字段映射正确。"""
    d = parse_problem_markdown(CONTRACT_MD)
    assert d is not None
    assert d["title"] == "Move Zeroes"
    assert d["topic"] == "数组"
    assert d["difficulty"] == "easy"
    assert "0 移动到末尾" in d["description"]
    assert "class Solution" in d["starter_code"]
    assert "slow" in d["optimal_solution"]
    assert isinstance(d["test_cases"], list)


def test_parse_problem_markdown_empty_returns_none():
    assert parse_problem_markdown("") is None
    assert parse_problem_markdown(None) is None


def test_parse_problem_markdown_no_sections_returns_none():
    """有文本但无 ## 契约节 → None。"""
    assert parse_problem_markdown("just some log output, no contract") is None


def test_parse_problem_markdown_strips_fences():
    """starter_code / optimal_solution 里的 ```python 围栏被剥掉。"""
    d = parse_problem_markdown(CONTRACT_MD)
    assert "```" not in d["starter_code"]
    assert "```" not in d["optimal_solution"]


def test_parse_problem_markdown_handles_missing_separator():
    """没有 ==== 分隔也照样解析（直接吃整段 body）。"""
    body = "## Title\nTwo Sum\n## Topic\n数组\n## Difficulty\nmedium\n"
    d = parse_problem_markdown(body)
    assert d["title"] == "Two Sum"
    assert d["difficulty"] == "medium"


def test_run_skill_cli_arguments_mode_builds_cmd():
    """$ARGUMENTS 模式：整段题面直接作为 -a 值，不拼成 key=value。"""
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _fake_proc(CONTRACT_MD)

    desc = "给定一个整数数组 nums 和 target，找出和为 target 的两个下标。"
    with patch("code_tutor_agent.agents.skill_cli.subprocess.run", side_effect=_fake_run):
        run_skill_cli("cta-generate-solution", {"$ARGUMENTS": desc})
    # -a 后是整段题面，且不应出现 key= 形式
    assert captured["cmd"][3] == "-a"
    assert captured["cmd"][4] == desc
    assert "=" not in captured["cmd"][4]


def test_generate_detailed_solution_via_skill_sync():
    """sync 核心：CLI 成功返回 stdout 原文（已 strip），失败转 error JSON。"""
    md = "# 思路一：暴力\n## Code\n```python\nclass Solution: ...\n```"
    with patch(
        "code_tutor_agent.agents.skill_cli.run_skill_cli",
        return_value=SkillResult(
            skill_name="cta-generate-solution", ok=True, output="  " + md + "\n",
        ),
    ):
        out = skill_cli.generate_detailed_solution_via_skill_sync("题面")
    assert out == md  # 已 strip

    with patch(
        "code_tutor_agent.agents.skill_cli.run_skill_cli",
        return_value=SkillResult(
            skill_name="cta-generate-solution", ok=False, error="boom",
        ),
    ):
        out = skill_cli.generate_detailed_solution_via_skill_sync("题面")
    assert "error" in json.loads(out)


def test_parse_problem_markdown_parses_function_signature():
    """解析 ## FunctionSignature 节 → function_signature 字段（修复：此前漏解析该节）。"""
    md = (
        "## Title\nNumber of Islands\n"
        "## Topic\n图\n"
        "## Difficulty\neasy\n"
        "## Description\n网格岛屿计数\n"
        "## Examples\nExample 1:\nInput: grid = [[1,1],[1,0]]\nOutput: 3\n"
        "## Constraints\n1 <= m <= 100\n"
        "## StarterCode\n```python\n"
        "class Solution:\n    def numIslands(self, grid: List[List[int]]) -> int:\n        pass\n"
        "```\n"
        "## FunctionSignature\ngrid: List[List[int]] -> int\n"
        "## OptimalSolution\n```python\n"
        "class Solution:\n    def numIslands(self, grid):\n        pass\n"
        "```\n"
    )
    d = parse_problem_markdown(md)
    assert d is not None
    assert d["function_signature"] == "grid: List[List[int]] -> int"
