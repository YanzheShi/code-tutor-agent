"""判题全链路集成测试：by-problem 建会话 → /run → /submit → verdict + SSE 契约。

设计（2026-09-06 测试加固 P1）：
- **零 LLM 依赖**：题目用 save_problem 种子数据（绕过出题），判题走真 runner。
  秒级可跑，不会因上游 LLM 故障假红（区别于 test_multi_question 的真 LLM 路径）。
- 覆盖此前完全没测的主链路：test_judge_flow.py 只测会话创建/画像结构，
  run/submit/verdict 这条核心链路一直没有集成断言。
- SSE 契约断言：
  - /progress/stream 对题目就绪的会话应立即推 `event: done`（含完整 state）；
  - /chat/stream 的 token 流格式 `data: {"t": ...}` + 终止符 `__DONE__`
    （mock 掉 agent_dialog 的 LLM，意图判定确定性返回）。
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from code_tutor_agent.api.main import app  # noqa: E402
from code_tutor_agent.db import database as db  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _agent_helpers import auth_headers  # noqa: E402

# ── 种子题目：两数之和（2 可见 + 1 隐藏用例）──
STARTER = '''class Solution:
    def twoSum(self, nums: List[int], target: int) -> List[int]:
        pass
'''
CORRECT = '''from typing import List

class Solution:
    def twoSum(self, nums: List[int], target: int) -> List[int]:
        seen = {}
        for i, x in enumerate(nums):
            if target - x in seen:
                return [seen[target - x], i]
            seen[x] = i
        return []
'''
WRONG = '''from typing import List

class Solution:
    def twoSum(self, nums: List[int], target: int) -> List[int]:
        return [0, 1]
'''
TEST_CASES = [
    {"input_args": ["[2,7,11,15]", "9"], "expected_output": "[0, 1]", "explanation": "示例1", "is_hidden": False},
    {"input_args": ["[3,2,4]", "6"], "expected_output": "[1, 2]", "explanation": "示例2", "is_hidden": False},
    {"input_args": ["[3,3]", "6"], "expected_output": "[0, 1]", "explanation": "隐藏", "is_hidden": True},
]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def problem_id(client):
    """种子一道题（module 级共享，库是 conftest 环境的临时库）。"""
    pid, _created = db.save_problem({
        "title": f"E2E 两数之和(判题链路)",
        "topic": "数组",
        "difficulty": "easy",
        "description": "找和为目标值的两个数的下标。",
        "test_cases": TEST_CASES,
        # save_problem 的 visible_test_cases 缺省会退化为全量 test_cases（含隐藏），
        # 必须显式传入非隐藏子集（生产由出题链路显式生成）
        "visible_test_cases": [tc for tc in TEST_CASES if not tc["is_hidden"]],
        "starter_code": STARTER,
        "optimal_solution": CORRECT,
        "brute_solution": CORRECT,
        "function_signature": "nums: List[int], target: int -> List[int]",
        "source": "generated",
    })
    return pid


def _by_problem_session(client, pid) -> str:
    resp = client.post(f"/session/by-problem/{pid}", headers=auth_headers(client))
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


class TestByProblemSession:
    def test_session_ready_immediately(self, client, problem_id):
        """by-problem 会话应直接就绪（awaiting_submit + 题目非空）。"""
        resp = client.post(f"/session/by-problem/{problem_id}", headers=auth_headers(client))
        data = resp.json()
        assert data["status"] == "awaiting_submit"
        assert data["problem"]["title"].startswith("E2E 两数之和")
        # 可见用例 = 非隐藏的 2 条
        assert len(data["problem"]["visible_test_cases"]) == 2
        assert data["problem"]["starter_code"].startswith("class Solution")

    def test_nonexistent_problem_404(self, client):
        resp = client.post("/session/by-problem/99999999", headers=auth_headers(client))
        assert resp.status_code == 404


class TestProgressStreamSSE:
    def test_ready_session_emits_done_event(self, client, problem_id):
        """/progress/stream 对就绪会话应立即推 event: done（含完整 state）。"""
        sid = _by_problem_session(client, problem_id)
        with client.stream(
            "GET", f"/session/{sid}/progress/stream", headers=auth_headers(client)
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            buffer = ""
            done_payload = None
            for chunk in resp.iter_text():
                buffer += chunk
                # SSE 按空行切事件
                while "\n\n" in buffer:
                    raw, buffer = buffer.split("\n\n", 1)
                    lines = raw.split("\n")
                    event = next((l[6:].strip() for l in lines if l.startswith("event:")), "message")
                    data_line = next((l[5:].strip() for l in lines if l.startswith("data:")), "")
                    if event == "done" and data_line:
                        done_payload = json.loads(data_line)
                        break
                if done_payload:
                    break
        assert done_payload is not None, "未收到 done 事件"
        assert done_payload["problem"]["title"].startswith("E2E 两数之和")
        assert done_payload["status"] == "awaiting_submit"

    def test_progress_stream_requires_auth(self, client, problem_id):
        sid = _by_problem_session(client, problem_id)
        resp = client.get(f"/session/{sid}/progress/stream")
        assert resp.status_code == 401


class TestRunFlow:
    def test_run_correct_code_all_passed(self, client, problem_id):
        """正确解法 → all_passed=True，逐用例 passed 且有 runtime。"""
        sid = _by_problem_session(client, problem_id)
        resp = client.post(
            f"/session/{sid}/run",
            json={"code": CORRECT, "language": "python"},
            headers=auth_headers(client),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["all_passed"] is True
        assert data["total"] == 2 and data["passed"] == 2
        for r in data["results"]:
            assert r["passed"] is True
            assert r["status"] == "Passed"
            assert isinstance(r.get("runtime_ms"), (int, float))

    def test_run_wrong_code_fails_with_detail(self, client, problem_id):
        """错误解法 → all_passed=False，失败用例带期望/实际输出。"""
        sid = _by_problem_session(client, problem_id)
        resp = client.post(
            f"/session/{sid}/run",
            json={"code": WRONG, "language": "python"},
            headers=auth_headers(client),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["all_passed"] is False
        assert data["passed"] < data["total"]
        failed = [r for r in data["results"] if not r["passed"]]
        assert failed, "至少应有一个失败用例"
        sample = failed[0]
        # last_run_results 的字段名是 expected（前端契约），与 judge_results 的
        # expected_output 不同——两个字段名历史上并存，断言两者兼容其一
        assert sample.get("expected") or sample.get("expected_output"), "失败用例应带期望输出"
        assert sample["status"] in ("Wrong Answer", "WA", "Runtime Error")

    def test_run_rejected_without_problem(self, client):
        """无题目的空会话不允许 run（400 而不是 500）。"""
        resp = client.post("/session", json={"topic": "数组", "difficulty": "easy"},
                           headers=auth_headers(client))
        sid = resp.json()["session_id"]
        resp = client.post(
            f"/session/{sid}/run",
            json={"code": CORRECT, "language": "python"},
            headers=auth_headers(client),
        )
        assert resp.status_code == 400

    def test_run_nonexistent_session_404(self, client):
        resp = client.post(
            f"/session/{uuid.uuid4()}/run",
            json={"code": CORRECT, "language": "python"},
            headers=auth_headers(client),
        )
        assert resp.status_code == 404


class TestSubmitFlow:
    def test_submit_ac(self, client, problem_id):
        """正确解法提交 → verdict=AC，tutor 有反馈，state.last_verdict=AC。"""
        sid = _by_problem_session(client, problem_id)
        resp = client.post(
            f"/session/{sid}/submit",
            json={"code": CORRECT, "language": "python"},
            headers=auth_headers(client),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["verdict"] == "AC"
        assert data["session_id"] == sid
        # state 落库校验：last_verdict 是权威字段；state.submissions 的快照里
        # verdict 字段恒为空串（判题前生成），判题结论在 judge_results 里
        state = client.get(f"/session/{sid}/state", headers=auth_headers(client)).json()
        assert state["last_verdict"] == "AC"
        assert any(
            jr.get("status") == "AC"
            for s in state["submissions"]
            for jr in (s.get("judge_results") or [])
        ), "judge_results 中应包含 AC"

    def test_submit_wrong_code_wa(self, client, problem_id):
        """错误解法提交 → verdict 非 AC（WA），状态保活可再提交。"""
        sid = _by_problem_session(client, problem_id)
        resp = client.post(
            f"/session/{sid}/submit",
            json={"code": WRONG, "language": "python"},
            headers=auth_headers(client),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["verdict"] in ("WA", "Wrong Answer", None)
        assert data["verdict"] != "AC"
        # 会话保活：WA 后可继续 run（判题节点 DB 异常兜底契约）
        resp = client.post(
            f"/session/{sid}/run",
            json={"code": CORRECT, "language": "python"},
            headers=auth_headers(client),
        )
        assert resp.status_code == 200
        assert resp.json()["all_passed"] is True

    def test_submit_nonexistent_session_404(self, client):
        resp = client.post(
            f"/session/{uuid.uuid4()}/submit",
            json={"code": CORRECT, "language": "python"},
            headers=auth_headers(client),
        )
        assert resp.status_code == 404


class TestChatStreamSSE:
    @pytest.fixture()
    def mock_dialog_llm(self, monkeypatch):
        """mock agent_dialog 的 LLM：意图判定确定性返回（非 ready，固定话术）。"""
        from code_tutor_agent.agents import agent_dialog

        class _FakeStructured:
            def invoke(self, messages):
                return agent_dialog.DialogIntent(
                    topic="数组", difficulty="",
                    is_ready=False,
                    next_message="好的，数组方向！你想从 Easy 开始还是 Medium？",
                )

        class _FakeLLM:
            def with_structured_output(self, schema):
                return _FakeStructured()

        monkeypatch.setattr(agent_dialog, "get_llm", lambda *a, **k: _FakeLLM())
        # 画像/记忆注入走 DB，测试库里可能无数据，置空提速
        monkeypatch.setattr(agent_dialog, "_build_profile_summary", lambda *a, **k: "")
        monkeypatch.setattr(agent_dialog, "_build_memory_summary", lambda *a, **k: "")

    def test_chat_stream_token_contract(self, client, mock_dialog_llm):
        """chat/stream 的 SSE 契约：data: {"t": token} 分片 + __DONE__ 终止，
        token 拼接 == intent.next_message（integrity of streamed text）。"""
        resp = client.post("/session", json={"topic": "数组", "difficulty": "easy"},
                           headers=auth_headers(client))
        sid = resp.json()["session_id"]
        resp = client.post(
            f"/session/{sid}/chat/stream",
            json={"message": "我想练数组"},
            headers=auth_headers(client),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        text = resp.read().decode("utf-8") if hasattr(resp, "read") else b"".join(
            resp.iter_bytes()).decode("utf-8")
        events = [l[5:].strip() for l in text.split("\n") if l.startswith("data:")]
        assert events, "无 SSE data 行"
        tokens = [json.loads(e)["t"] for e in events]
        assert tokens[-1] == "__DONE__", f"最后一个事件应为 __DONE__ 终止符，实际: {tokens[-1]!r}"
        body_tokens = [t for t in tokens[:-1]]
        assert body_tokens, "应至少有一个 token 分片"
        joined = "".join(body_tokens)
        assert "数组方向" in joined and "Easy" in joined, f"流式拼接内容异常: {joined!r}"
