"""Judge0 判题沙箱冒烟测试（可选，CI 安全）。

仅当环境变量 JUDGE0_URL 指向真实 Judge0 服务时才真跑（如 docker compose
--profile judge0 起的本 地服务）；未配置则整类 skip，不影响常规回归：

    JUDGE0_URL=http://localhost:2358 uv run pytest tests/integration/test_judge0_smoke.py -v

覆盖：健康检查 → 单提交（stdout 回显）→ 测试用例批判（AC + WA 各一）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from code_tutor_agent.sandbox import judge0_client  # noqa: E402

JUDGE0_URL = os.getenv("JUDGE0_URL", "")
pytestmark = pytest.mark.skipif(
    not JUDGE0_URL,
    reason="JUDGE0_URL 未配置——judge0 是可选 profile，仅在显式提供时冒烟",
)


def test_judge0_health():
    info = judge0_client.check_health()
    assert info, "check_health 应返回非空信息"


def test_judge0_single_submission_echo():
    """单提交：print 回显，断言 stdout。"""
    res = judge0_client.run_code("print('cta-smoke-ok')")
    assert res.stdout.strip() == "cta-smoke-ok"


def test_judge0_batch_ac_and_wa():
    """批判：Solution 两数之和，一 AC 一 WA（对拍驱动器真实走一遍）。"""
    solution = """from typing import List

class Solution:
    def twoSum(self, nums: List[int], target: int) -> List[int]:
        seen = {}
        for i, x in enumerate(nums):
            if target - x in seen:
                return [seen[target - x], i]
            seen[x] = i
        return []
"""
    cases = [
        {"input_args": ["[2,7,11,15]", "9"], "expected_output": "[0, 1]", "is_hidden": False},
        {"input_args": ["[3,2,4]", "6"], "expected_output": "[9, 9]", "is_hidden": False},  # 故意错 → WA
    ]
    results = judge0_client.submit_test_cases(solution, cases)
    assert len(results) == 2
    s0 = results[0].get("status", "")
    s1 = results[1].get("status", "")
    assert s0 not in ("Judge Error", "Judge0 不可用"), f"沙箱故障而非用例判定: {s0}"
    assert s1 != s0, f"一 AC 一 WA 状态不应相同: {s0!r} vs {s1!r}"
    assert s0.lower() in ("accepted", "passed", "ac"), f"用例1 应 AC，实际 {s0!r}"
