"""编译错误指针（CE 指示器）测试（2026-09-09，Phase 0-2）。

覆盖：
- check_compile：合法代码 None、基础语法错、未闭合三引号（用户实锤 badcase，
  断言无 harness 内部代码泄露）、Tab 展开后指针列对齐；
- run_solution 预检短路：N 个用例返回同一 CE payload（force_local，零子进程）；
- judge0_client.submit_test_cases 直连短路（不碰网络）；
- verdict 归约：Compile Error → "CE"（_deterministic_verdict / _build_base_result）；
- _to_run_results 的 compile_error 透传。

不依赖 LLM / Judge0 服务。
"""
from __future__ import annotations

from code_tutor_agent.sandbox.compile_check import COMPILE_ERROR_STATUS, check_compile
from code_tutor_agent.sandbox.runner import RunnerResult, run_solution


# ── check_compile ────────────────────────────────────────────────


class TestCheckCompile:
    def test_valid_code_returns_none(self):
        assert check_compile("class Solution:\n    def f(self):\n        return 1\n") is None

    def test_basic_syntax_error(self):
        info = check_compile("def f(:\n    pass\n")
        assert info is not None
        assert info["status"] == COMPILE_ERROR_STATUS
        assert info["line"] == 1
        assert info["message"]
        assert "^" in info["pointer"]
        # human 降级串：含行号 + 源码行 + 指针
        assert "Line 1" in info["human"]
        assert info["text"] in info["human"]
        assert info["pointer"] in info["human"]

    def test_pointer_column_alignment(self):
        # 错误在第二个 "="（raw col 7）："\tx = = 1" → Tab 展开(4) 后 "        x = = 1"
        info = check_compile("if True:\n\t\tx = = 1\n")
        assert info is not None
        assert info["line"] == 2
        assert info["text"] == "        x = = 1"  # Tab 已展开
        assert info["pointer"][12] == "^"          # 展开后第二个 "=" 下方
        assert info["pointer"].count("^") == 1

    def test_unclosed_triple_quote_no_harness_leak(self):
        """用户实锤 badcase：末尾未闭合 `\"\"\"` —— 用户代码单独编译，
        行号是用户行号，且绝不出现 harness 内部代码（RESULT / json.dumps）。"""
        code = (
            "def f():\n"
            "    return 1\n"
            "\n"
            '"""\n'
            "cur_remain = 6\n"
        )
        info = check_compile(code)
        assert info is not None
        assert info["status"] == COMPILE_ERROR_STATUS
        assert info["line"] == 4  # 用户编辑器行号，非 harness 行号
        # 无 harness 泄露（本地路径根本没有 harness 参与编译）
        assert "RESULT" not in info["human"]
        assert "json.dumps" not in info["human"]
        assert "test_cases" not in info["human"]

    def test_recursion_error_downgraded(self, monkeypatch):
        """深嵌套解析炸弹 → RecursionError 降级为普通 CE（不打穿判题链路）。
        构造 200 层嵌套括号触发 RecursionError（限长闸门挡不住的紧凑炸弹）。"""
        code = "x = " + "(" * 300 + "1" + ")" * 300
        info = check_compile(code)
        # 要么触发 RecursionError 降级，要么本身是语法/内存正常解析（不同解释器栈深），
        # 但绝不能抛异常出去
        assert info is None or info["status"] == COMPILE_ERROR_STATUS


# ── run_solution 预检短路 ────────────────────────────────────────


class TestRunSolutionShortCircuit:
    def test_all_cases_share_compile_error_payload(self):
        tcs = [
            {"input_args": ["1"], "expected_output": "1"},
            {"input_args": ["2"], "expected_output": "2"},
            {"input_args": ["3"], "expected_output": "3"},
        ]
        results = run_solution("def broken(:\n    pass\n", tcs, force_local=True)
        assert len(results) == 3
        for r in results:
            assert r.status == COMPILE_ERROR_STATUS
            assert r.compile_error is not None
            assert r.compile_error["line"] == 1
        # 同一 payload（同一 dict 内容）
        assert all(r.compile_error == results[0].compile_error for r in results)
        # detail 是 human 降级串
        assert "Line 1" in results[0].detail
        # to_dict 带上 compile_error（序列化契约）
        assert "compile_error" in results[0].to_dict()

    def test_valid_code_still_runs_passed(self):
        tcs = [{"input_args": [], "expected_output": "5"}]
        results = run_solution(
            "class Solution:\n    def f(self):\n        return 5\n", tcs, force_local=True,
        )
        assert len(results) == 1
        assert results[0].status == "Passed"
        assert results[0].compile_error is None

    def test_runner_result_dict_without_ce_has_no_key(self):
        r = RunnerResult(0, "Passed", detail="x")
        assert "compile_error" not in r.to_dict()


# ── judge0_client 直连短路（不碰网络）──────────────────────────


class TestJudge0ShortCircuit:
    def test_submit_test_cases_short_circuits_on_ce(self):
        from code_tutor_agent.sandbox.judge0_client import submit_test_cases

        tcs = [{"input_args": ["1"], "expected_output": "1"},
               {"input_args": ["2"], "expected_output": "2"}]
        results = submit_test_cases("def f(:\n    pass\n", tcs)
        assert len(results) == 2
        for r in results:
            assert r["status"] == COMPILE_ERROR_STATUS
            assert r["compile_error"]["line"] == 1
            assert "compile_error" in r


# ── verdict 归约与 _to_run_results 透传 ─────────────────────────


class TestVerdictAndDataFlow:
    def test_deterministic_verdict_ce(self):
        from code_tutor_agent.agents.agent_judge import _deterministic_verdict

        results = [RunnerResult(i, COMPILE_ERROR_STATUS) for i in range(3)]
        assert _deterministic_verdict(results) == "CE"

    def test_build_base_result_maps_ce(self):
        from code_tutor_agent.nodes.agent_judge import _build_base_result

        results = [RunnerResult(0, COMPILE_ERROR_STATUS, detail="Line 1: invalid syntax")]
        base = _build_base_result(results, [])
        assert base.status == "CE"
        assert "invalid syntax" in base.detail

    def test_to_run_results_passes_compile_error_through(self):
        from code_tutor_agent.nodes.agent_judge import _to_run_results

        ce = check_compile("def f(:\n")
        results = [RunnerResult(0, COMPILE_ERROR_STATUS, detail="x", compile_error=ce)]
        out = _to_run_results(results, [])
        assert out[0]["compile_error"] == ce

    def test_to_run_results_none_when_not_ce(self):
        from code_tutor_agent.nodes.agent_judge import _to_run_results

        results = [RunnerResult(0, "Passed", detail="ok")]
        out = _to_run_results(results, [])
        assert out[0]["compile_error"] is None

    def test_pydantic_run_result_keeps_compile_error(self):
        """Phase 2 关键回归：RunResult pydantic 必须显式声明字段，
        否则 last_run_results dict → RunCodeResponse 时 compile_error 被静默丢弃。"""
        from code_tutor_agent.schemas.api import RunResult as PydanticRunResult

        ce = check_compile("def f(:\n")
        m = PydanticRunResult(
            test_case_id=0, passed=False, status=COMPILE_ERROR_STATUS,
            detail="x", compile_error=ce,
        )
        assert m.model_dump()["compile_error"] == ce
