"""延迟探针（按需手动跑，默认不会被收集）：真 LLM 下量「点提交到看到结果」的秒数。

命名以 `_` 开头 → pytest 默认不收集，不会把真网关的不确定性带进日常套件。
放这个目录是为了复用隔离 schema（tests/conftest 建一次性 schema、会话结束 DROP
CASCADE），不会写进开发库。

用法：pytest tests/integration/_probe_submit_latency.py -q -s
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from code_tutor_agent.api.main import app  # noqa: E402
from code_tutor_agent.db import database as db  # noqa: E402
from _agent_helpers import auth_headers  # noqa: E402

from test_run_submit_flow import CORRECT, STARTER, TEST_CASES, WRONG  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _seed() -> int:
    pid, _ = db.save_problem({
        "title": "探针 两数之和", "topic": "数组", "difficulty": "easy",
        "description": "找和为目标值的两个数的下标。",
        "test_cases": TEST_CASES,
        "visible_test_cases": [tc for tc in TEST_CASES if not tc["is_hidden"]],
        "starter_code": STARTER, "optimal_solution": CORRECT, "brute_solution": CORRECT,
        "function_signature": "nums: List[int], target: int -> List[int]",
        "source": "generated",
    })
    return pid


def _poll_state(client, sid, want_copy: bool, limit: float):
    """返回 (耗时秒, 拿到的文案)。want_copy=False 时只等判题结果落库。"""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < limit:
        st = client.get(f"/session/{sid}/state", headers=auth_headers(client)).json()
        copy = (st.get("warm_feedback") or "").strip()
        if not want_copy:
            return time.perf_counter() - t0, ""
        if copy:
            return time.perf_counter() - t0, copy[:60]
        time.sleep(0.2)
    return float("inf"), "(超时未见文案)"


@pytest.mark.parametrize("code,label", [(WRONG, "WA"), (CORRECT, "AC")])
def test_probe(client, code, label):
    sid = client.post(f"/session/by-problem/{_seed()}",
                      headers=auth_headers(client)).json()["session_id"]

    t0 = time.perf_counter()
    resp = client.post(f"/session/{sid}/submit",
                       json={"code": code, "language": "python"},
                       headers=auth_headers(client))
    t_submit = time.perf_counter() - t0
    body = resp.json()

    # 文案到达的总时长 = submit 已花掉的 + 之后轮询到非空的
    rest, preview = _poll_state(client, sid, want_copy=True, limit=120)
    t_copy = t_submit + rest

    print(
        f"\n[探针 {label}] POST /submit 阻塞 {t_submit:.1f}s → verdict={body['verdict']} "
        f"tutor_pending={body['tutor_pending']}\n"
        f"           辅导文案总耗时 {t_copy:.1f}s（其中判题 {t_submit:.1f}s + 等待 {rest:.1f}s）"
        f" 预览: {preview!r}"
    )
    assert body["verdict"], "verdict 必须随 submit 一起返回"
