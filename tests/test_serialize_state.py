"""回归测试：serialize_state 必须能序列化含 ProblemAttemptRecord 的状态。

触发场景（2026-07-21）：出题后 SSE 进度流在 json.dumps(serialize_state(state))
阶段抛 `TypeError: Object of type ProblemAttemptRecord is not JSON serializable`，
因为 serialize_state 直接把 problem_history（Pydantic 对象列表）原样返回。
"""
import json

from code_tutor_agent.api.serializers import serialize_state
from code_tutor_agent.schemas.state import ProblemAttemptRecord, SessionPhase


def _sample_record() -> ProblemAttemptRecord:
    return ProblemAttemptRecord(
        problem_id=1,
        title="Two Sum",
        difficulty="easy",
        verdict="AC",
    )


def test_serialize_state_handles_problem_attempt_record():
    state = {
        "mode": "agent",
        "status": "dialog",
        "phase": SessionPhase.solving,
        "problem_history": [_sample_record(), _sample_record()],
    }
    out = serialize_state(state)
    # 不应抛 TypeError
    payload = json.dumps(out, ensure_ascii=False)
    assert "problem_history" in out
    # 每个 ProblemAttemptRecord 都应被转成 dict
    assert all(isinstance(r, dict) for r in out["problem_history"])
    assert out["problem_history"][0]["title"] == "Two Sum"
    assert json.loads(payload)["problem_history"][0]["problem_id"] == 1


def test_serialize_state_accepts_empty_or_dict_problem_history():
    # 空列表
    assert serialize_state({"problem_history": []})["problem_history"] == []
    # 已是 dict 的列表（兼容历史/外部来源）
    raw = [{"problem_id": 1, "title": "x", "difficulty": "easy", "verdict": "AC"}]
    assert serialize_state({"problem_history": raw})["problem_history"] == raw
