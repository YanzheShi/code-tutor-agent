"""submit 拆链路端到端（2026-10-27）：判题秒回 → 后台补写文案 → 会话仍可继续提交。

这条链路真正危险的地方不是「文案晚点到」，而是**后台那次写回会不会踩掉
wait_for_submit 的挂起中断**。历史上同一位置出过两次事故（2026-08-04、2026-10-01）：
在 interrupt 挂起期直接 update_state 会丢掉挂起任务，此后 Command(resume) 空转、
/run 一律 400，整道题永久判不了。所以本文件的主断言是「写回之后还能再提交」。

与 test_run_submit_flow.py 同口径：零 LLM 依赖（mock analyze_judge_results）、
真 runner + 真 PostgresSaver checkpoint，秒级可跑。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from code_tutor_agent.agents.agent_judge import JudgeAnalysis  # noqa: E402
from code_tutor_agent.api.main import app  # noqa: E402
from code_tutor_agent.db import database as db  # noqa: E402
from _agent_helpers import auth_headers  # noqa: E402

from test_run_submit_flow import (  # noqa: E402  复用种子题与代码样本
    CORRECT,
    STARTER,
    TEST_CASES,
    WRONG,
)

WA_TEXT = "延迟生成的点评：看看边界用例"
AC_TEXT = "延迟生成的祝贺：全部通过 🎉"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="function")
def problem_id(client):
    pid, _created = db.save_problem({
        "title": "E2E 两数之和(拆链路)",
        "topic": "数组",
        "difficulty": "easy",
        "description": "找和为目标值的两个数的下标。",
        "test_cases": TEST_CASES,
        "visible_test_cases": [tc for tc in TEST_CASES if not tc["is_hidden"]],
        "starter_code": STARTER,
        "optimal_solution": CORRECT,
        "brute_solution": CORRECT,
        "function_signature": "nums: List[int], target: int -> List[int]",
        "source": "generated",
    })
    return pid


def _session(client, pid) -> str:
    resp = client.post(f"/session/by-problem/{pid}", headers=auth_headers(client))
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def _state(client, sid) -> dict:
    resp = client.get(f"/session/{sid}/state", headers=auth_headers(client))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _analysis(**kwargs) -> JudgeAnalysis:
    """按 forced_verdict 出确定文案，让 mock 的返回与真实判题结论一致。

    只吃 kwargs：``build_tutor_update`` 就是以全关键字参数调用 analyze_judge_results 的。
    """
    verdict = "AC" if kwargs.get("forced_verdict") == "AC" else "WA"
    return JudgeAnalysis(
        verdict=verdict,
        warm_feedback=AC_TEXT if verdict == "AC" else WA_TEXT,
        repair_suggestion="" if verdict == "AC" else "检查两数之和的边界",
        should_retry=(verdict != "AC"),
    )


def _wait_for_tutor(client, sid, *, timeout=15.0) -> dict:
    """像前端那样轮询 /state，直到后台把 warm_feedback 补上。

    判据用 warm_feedback：它是后台那一次写入唯一的产物，判题时已被显式置空。
    """
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = _state(client, sid)
        if (last.get("warm_feedback") or "").strip():
            return last
        time.sleep(0.3)
    pytest.fail(f"后台辅导文案未在 {timeout}s 内落库，最后状态={last.get('status')}/"
                f"warm_feedback={last.get('warm_feedback')!r}")


class TestSubmitDefersTutorCopy:
    def test_wa_judge_returns_immediately_then_copy_lands(self, client, problem_id):
        sid = _session(client, problem_id)

        with patch("code_tutor_agent.nodes.agent_tutor.analyze_judge_results",
                   side_effect=_analysis):
            resp = client.post(f"/session/{sid}/submit",
                               json={"code": WRONG, "language": "python"},
                               headers=auth_headers(client))
            assert resp.status_code == 200, resp.text
            data = resp.json()

            # ① 请求里只等到判题：verdict 有、文案还没
            assert data["verdict"] not in (None, "AC"), data
            assert data["tutor_pending"] is True
            assert data["tutor_message"] is None

            # ② 会话已回到等待提交（不是卡死在 judging）
            assert data["status"] == "awaiting_submit", data

            # ③ 后台补写后，前端轮询就能看到（status 不能被这次写回改掉）
            st = _wait_for_tutor(client, sid)
            assert st["warm_feedback"] == WA_TEXT
            assert st["tutor_messages"][-1]["content"].startswith(WA_TEXT)
            assert st["status"] == "awaiting_submit"

    def test_ac_copy_is_also_deferred(self, client, problem_id):
        sid = _session(client, problem_id)

        with patch("code_tutor_agent.nodes.agent_tutor.analyze_judge_results",
                   side_effect=_analysis):
            resp = client.post(f"/session/{sid}/submit",
                               json={"code": CORRECT, "language": "python"},
                               headers=auth_headers(client))
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["verdict"] == "AC"
            assert data["tutor_pending"] is True

            st = _wait_for_tutor(client, sid)
            assert st["warm_feedback"] == AC_TEXT
            # AC 的 phase 仍由 critic 定为 reviewing（写回只补文案，不动路由字段）
            assert st["phase"] == "reviewing", st["phase"]

    def test_session_still_judges_after_background_write(self, client, problem_id):
        """核心回归闸门：后台写回必须保住挂起的 interrupt，第二次提交仍能真判题。

        丢中断的症状是 Command(resume) 空转 —— verdict 不再更新、只回旧状态。
        """
        sid = _session(client, problem_id)

        with patch("code_tutor_agent.nodes.agent_tutor.analyze_judge_results",
                   side_effect=_analysis):
            first = client.post(f"/session/{sid}/submit",
                                json={"code": WRONG, "language": "python"},
                                headers=auth_headers(client)).json()
            assert first["verdict"] != "AC"
            _wait_for_tutor(client, sid)

            second = client.post(f"/session/{sid}/submit",
                                 json={"code": CORRECT, "language": "python"},
                                 headers=auth_headers(client))
            assert second.status_code == 200, second.text
            body = second.json()
            assert body["verdict"] == "AC", f"写回后二次提交没能判题：{body}"

            st = _wait_for_tutor(client, sid)
            assert st["warm_feedback"] == AC_TEXT
            # 两轮各一条点评，晚到的那一条不会顶掉先写好的
            tutor_texts = [m["content"] for m in st["tutor_messages"]]
            assert any(t.startswith(WA_TEXT) for t in tutor_texts), tutor_texts
            assert any(t.startswith(AC_TEXT) for t in tutor_texts), tutor_texts

    def test_run_after_deferred_submit_is_unaffected(self, client, problem_id):
        """/run（sample）不延迟、不进后台写回，拆链路不能把它一起拖下水。"""
        sid = _session(client, problem_id)
        with patch("code_tutor_agent.nodes.agent_tutor.analyze_judge_results",
                   side_effect=_analysis):
            resp = client.post(f"/session/{sid}/submit",
                               json={"code": WRONG, "language": "python"},
                               headers=auth_headers(client))
            assert resp.json()["tutor_pending"] is True
            # 等后台写回结束再去 run：两次 graph 写并发会互相踩 checkpoint，
            # 这里要测的是「拆链路有没有改变 run 的语义」，不是并发压力。
            _wait_for_tutor(client, sid)

        resp = client.post(f"/session/{sid}/run",
                           json={"code": CORRECT, "language": "python"},
                           headers=auth_headers(client))
        assert resp.status_code == 200, resp.text
        assert resp.json()["all_passed"] is True


class TestSubmitGuards:
    """submit 的两道新闸：总超时、并发护栏。

    这两条是「接线」断言——判题段被拆出来后必须仍受闸管着，否则一次卡死的判题
    又会占满线程池（这正是当初 submit 独有的问题：/run 早就有护栏，submit 没有）。
    """

    def test_submit_returns_504_when_judging_overruns(self, client, problem_id, monkeypatch):
        import code_tutor_agent.api.routers.session as session_router

        from code_tutor_agent.sandbox.runner import RunnerResult

        sid = _session(client, problem_id)
        monkeypatch.setattr(session_router, "SUBMIT_TIMEOUT", 0.3)

        def _slow(*_a, **_k):
            time.sleep(1.5)
            return [RunnerResult(0, "Passed", "[0, 1]", runtime_ms=1.0)]

        monkeypatch.setattr("code_tutor_agent.nodes.agent_judge.run_solution", _slow)

        resp = client.post(f"/session/{sid}/submit",
                           json={"code": WRONG, "language": "python"},
                           headers=auth_headers(client))
        assert resp.status_code == 504, resp.text
        # 文案得是「稍后再看」而不是「提交失败」：判题线程仍在跑，结果会落 checkpoint
        assert "后台" in resp.json()["detail"]

    def test_submit_runs_inside_the_concurrency_guard(self, client, problem_id, monkeypatch):
        from fastapi import HTTPException

        import code_tutor_agent.api.routers.session as session_router

        sid = _session(client, problem_id)
        entered: list[bool] = []

        def _gate(fn, *a, **k):
            entered.append(True)
            raise HTTPException(429, "当前判题繁忙，请稍后再试")

        monkeypatch.setattr(session_router, "run_with_concurrency_limit", _gate)

        resp = client.post(f"/session/{sid}/submit",
                           json={"code": WRONG, "language": "python"},
                           headers=auth_headers(client))
        assert resp.status_code == 429, resp.text
        assert entered, "submit 必须经并发护栏执行判题，不能退回裸 asyncio.to_thread"
        assert resp.json()["detail"] == "当前判题繁忙，请稍后再试"
