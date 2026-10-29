"""Agent 判题节点 — 通过 Judge0 进行客观执行的 LangGraph 节点。

职责边界（2026-10-25 职责归位）：本节点只做「执行 + 归约权威 verdict + 落库
结构化结果 + 画像/路由标记」。**非 AC 的辅导文案不再在这里生成** —— 判题对辅导
只能交棒（写 ``state.last_exec_results``，由 ``agent_tutor_node`` 读它产出反馈）。

已知残留：AC 线的反馈（祝贺 + 复杂度分析 + 优化建议）仍由本节点调 LLM 生成，
因为 AC 分支在图上不经 ``agent_tutor_node``（full→update_profile、sample→wait_for_submit）。
若要连它一起归位，需把 tutor 改成多出口条件边，会冲击 ``deps.AS_NODE_AWAITING_SUBMIT``
锚点（2026-10-01「/run 永久 400」事故的支点），故暂缓。

运行/提交统一收口到本节点：
  - scope="sample"（运行按钮）→ 只跑 visible_test_cases，写诊断 last_run_results，不写画像
  - scope="full"（提交按钮）→ 跑全量用例（含边界），AC 写 v2 画像并 done

节点流转（由 graph.agent_judge_router 路由，本节点只返回 state 更新）：
  WA               → agent_tutor_node（交棒客观结果，辅导文案在那里生成）
  full + AC        → update_profile_node → critic_node
  sample + AC / 运行 → wait_for_submit_node（不写画像、不 done，保持循环）
"""

from __future__ import annotations

import logging
from typing import Any

from code_tutor_agent.agents.agent_judge import (
    JudgeAnalysis,
    analyze_judge_results,
    _deterministic_verdict,
)
from code_tutor_agent.db.database import get_problem_by_id
from code_tutor_agent.leetcode.leetcode_fetcher import extract_signature_from_solution
from code_tutor_agent.sandbox.runner import run_solution
from code_tutor_agent.schemas.state import JudgeResult, SessionState

logger = logging.getLogger(__name__)


def _m_judge_error(detail: str = "") -> None:
    """判题基础设施故障埋点（非侵入：失败静默）。只计 infra 错误，用户 WA/RE 不算。"""
    try:
        from code_tutor_agent.monitoring.metrics import get_registry

        reg = get_registry()
        reg.record("judge_error")
        streak = reg.record_streak("judge_fail", ok=False)
        if streak >= 3:
            logger.error("[metrics] judge infra failure streak=%d (last: %s)", streak, detail)
    except Exception:
        pass


def _m_judge_ok() -> None:
    """判题正常出结果（任意 verdict，含 WA/RE/TLE——那是教学结果不是故障）。"""
    try:
        from code_tutor_agent.monitoring.metrics import get_registry

        reg = get_registry()
        reg.record("judge_total")
        reg.record_streak("judge_fail", ok=True)
    except Exception:
        pass


def _param_names(func_sig: str) -> list[str]:
    """从函数签名提取参数名列表（跳过 self）。解析失败返回 []，调用方回退原始入参。

    兼容三种形态：
      1. ``def f(self, nums: List[int], k: int) -> ...``（optimal_solution 提取的 def 全文）
      2. ``(nums: List[int], k: int)``（仅参数段）
      3. ``nums: List[int], k: int -> List[int]``（LLM 直填的无括号形态，DB 里真实存在）
    类型注解里的逗号（List[List[int]]、Dict[str, int]）按括号深度跳过，不会切碎注解。
    """
    import re

    try:
        sig = (func_sig or "").strip()
        # 无括号形态：按 -> 切掉返回类型，把参数段当「伪括号内文」处理
        if "(" not in sig:
            sig = sig.split("->", 1)[0].strip()
            if not sig:
                return []
            inner = sig
        else:
            m = re.search(r"\(([^)]*)\)", sig)
            if not m:
                return []
            inner = m.group(1)
        parts: list[str] = []
        depth = 0
        cur: list[str] = []
        for ch in inner:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
        if cur:
            parts.append("".join(cur))
        names: list[str] = []
        for p in parts:
            p = p.strip()
            if not p or p == "self":
                continue
            name = p.split(":", 1)[0].split("=", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z_]\w*", name):
                names.append(name)
        return names
    except Exception:
        return []


def _named_input_args(func_sig: str, input_args: list | None) -> list[str]:
    """入参展示格式化：参数名与入参个数对齐时输出 ``nums = [1,3], k = 3`` 命名形态。

    个数不齐或解析失败时原样返回——宁可不命名，不给错位的「参数名 = 值」误导。
    仅用于展示边界（judge_results / last_run_results），不影响判题入参
    （harness 直读 DB 原始 test_cases）。"""
    args = [str(a) for a in (input_args or [])]
    names = _param_names(func_sig)
    if names and len(names) == len(args):
        return [f"{n} = {a}" for n, a in zip(names, args)]
    return args

# Timeout per test case during agent judging
AGENT_JUDGE_TIMEOUT = 5.0


def _to_run_results(results: list, test_cases: list, func_sig: str = "") -> list[dict]:
    """Convert RunnerResult list → RunCodeResponse-shaped dicts (mirrors old run.py)."""
    run_results: list[dict] = []
    for r in results:
        passed = r.status == "Passed"
        _vi = r.test_case_id
        tc = test_cases[_vi] if 0 <= _vi < len(test_cases) else {}
        run_results.append({
            "test_case_id": r.test_case_id,
            "passed": passed,
            "status": r.status,
            "detail": r.detail[:200] if r.detail else "",
            # RunnerResult 自身已带 input_args（执行时用到的真实输入），优先用它，
            # 避免依赖 test_cases[_vi] 索引错位或 tc 非 dict 时把 input 丢成空列表。
            # 展示层做命名格式化（nums = [...], k = 3），判题入参不受影响。
            "input_args": _named_input_args(
                func_sig,
                list(getattr(r, "input_args", None)
                     or (tc.get("input_args", []) if isinstance(tc, dict) else [])),
            ),
            "expected": tc.get("expected_output", "") if isinstance(tc, dict) else "",
            # 实际输出：本地 runner 一直带；Judge0 harness 2026-10-03 起 WA/RE 行也回传。
            # 运行 tab 失败用例的「实际 vs 期望」对比靠它，不再依赖 detail 里的 got= 文本。
            "actual": getattr(r, "actual_output", "") or "",
            "explanation": tc.get("explanation", "") if isinstance(tc, dict) else "",
            "runtime_ms": r.runtime_ms,
            "memory_kb": r.memory_kb,
            # 编译错误结构化 payload（^ 指针等）：非 CE 时为 None，不占 payload
            "compile_error": getattr(r, "compile_error", None),
        })
    return run_results


def _to_plain_results(results: list) -> list[dict]:
    """RunnerResult → 可进 checkpoint 的平面 dict（判题→辅导的交棒载荷）。

    只保留辅导节点生成文案要读的字段；``detail`` 截到 200 字符，与
    ``format_results_for_prompt`` 展示给 LLM 的长度完全一致——截更短会丢信息，
    截更长只会白白撑大 checkpoint。

    用 getattr 取值：本地 runner 与 Judge0 适配层返回的结果对象字段并不完全齐平
    （``_to_run_results`` 同样靠 getattr 兜），而本函数在节点里执行，抛异常会击穿
    整次 invoke → /submit 500、会话失去挂起节点（2026-09-04 同类事故）。
    """
    return [
        {
            "test_case_id": getattr(r, "test_case_id", 0) or 0,
            "status": getattr(r, "status", "") or "",
            "detail": (getattr(r, "detail", "") or "")[:200],
            "runtime_ms": getattr(r, "runtime_ms", 0.0) or 0.0,
            "memory_kb": getattr(r, "memory_kb", 0.0) or 0.0,
            "expected_output": getattr(r, "expected_output", "") or "",
            "actual_output": getattr(r, "actual_output", "") or "",
        }
        for r in results
    ]


def _resolve_inputs(state: SessionState) -> "dict | tuple":
    """校验输入并加载 problem + 测试用例。

    Returns:
        - dict: 错误 update（``{"status": "error", "error_message": ...}``），
          当任一前置条件不满足时直接返回，由路由 → END。
        - tuple: ``(code, is_run, problem_dict, test_cases)``，全部满足时。
    """
    if not state.submissions:
        logger.warning("No submission found — routing to error")
        return {"status": "error", "error_message": "No submission to judge"}

    last = state.submissions[-1]
    is_run = last.is_run
    code = last.code

    problem_id = state.problem.problem_id if state.problem else 0
    if not problem_id:
        logger.warning("No problem_id in state — routing to error")
        return {"status": "error", "error_message": "No problem loaded"}

    problem_dict = get_problem_by_id(problem_id)
    if not problem_dict:
        logger.warning("Problem %d not found in DB", problem_id)
        return {"status": "error", "error_message": f"Problem {problem_id} not found"}

    # ── 按 scope 选测试用例 ──
    if state.judge_scope == "sample":
        test_cases = problem_dict.visible_test_cases
        if not test_cases:
            # 兜底：从无 hidden 标记的用例里取可见集
            test_cases = [tc for tc in problem_dict.test_cases if not tc.get("is_hidden", False)]
    else:
        test_cases = problem_dict.test_cases

    # ── 最后兜底：回退会话 state 里的可见用例 ──
    # DB 里的用例可能为空（后台 build_suite 失败 / 复用旧题 / 老数据），
    # 而出题时 ProblemMeta.visible_test_cases 带着 LeetCode 示例（含 expected_output）。
    # 旧实现此时直接报 "No test cases available" → status=error → graph 走到 END，
    # 会话失去挂起节点（next=()），此后 /run 一律 400、/submit 空转返回开场白，
    # 整道题彻底判不了（题目 124 事故）。宁可用示例用例判，也不要判不了。
    if not test_cases:
        fallback = list(getattr(state.problem, "visible_test_cases", None) or [])
        if fallback:
            logger.warning(
                "Problem %d has no DB test cases — falling back to %d state visible cases",
                problem_id, len(fallback),
            )
            test_cases = [tc if isinstance(tc, dict) else dict(tc) for tc in fallback]
            if state.judge_scope != "sample":
                # 全量判题无 hidden 用例可跑时，退化为只判可见示例（保底可用）
                logger.warning("Problem %d: full-scope judge downgraded to visible cases", problem_id)

    if not test_cases:
        logger.warning("No test cases for problem %d", problem_id)
        return {"status": "error", "error_message": "No test cases available"}

    return code, is_run, problem_dict, test_cases


def _resolve_function_signature(problem_dict: Any) -> str:
    """解析 function_signature，并用 optimal_solution 提取的签名兜底覆盖。

    同 judge.py 和 run.py 的做法：当 DB 存的签名缺失/不准时，从参考解重新提取。
    """
    func_sig = getattr(problem_dict, "function_signature", "") or ""
    optimal_code = getattr(problem_dict, "optimal_solution", "") or ""
    if optimal_code:
        extracted = extract_signature_from_solution(optimal_code)
        if extracted and extracted != func_sig:
            logger.info("Overriding function_signature from optimal_solution: %s", extracted[:80])
            func_sig = extracted
    return func_sig


def _build_base_result(raw_results: list, test_cases: list | None = None, func_sig: str = "") -> JudgeResult:
    """由执行结果构造 base JudgeResult：首个失败用例（结构化）或 AC 汇总文案。"""
    _status_map = {
        "Passed": "AC",
        "Wrong Answer": "WA",
        "Runtime Error": "RE",
        "Compile Error": "CE",
        "TLE": "TLE",
        "Time Limit Exceeded": "TLE",
    }
    first_fail = next((r for r in raw_results if r.status != "Passed"), None)
    if first_fail is not None:
        _tc = test_cases[first_fail.test_case_id] if test_cases and 0 <= first_fail.test_case_id < len(test_cases) else {}
        return JudgeResult(
            status=_status_map.get(first_fail.status, "WA"),
            phase="base",
            detail=first_fail.detail or "",
            runtime_ms=first_fail.runtime_ms,
            memory_kb=first_fail.memory_kb,
            input_args=_named_input_args(func_sig, list(first_fail.input_args or [])),
            expected_output=first_fail.expected_output or "",
            actual_output=first_fail.actual_output or "",
            explanation=_tc.get("explanation", "") if isinstance(_tc, dict) else "",
            # CE 专属：结构化编译错误 payload 直通前端（编辑器红标 + CE 结果面板）
            compile_error=getattr(first_fail, "compile_error", None),
        )
    return JudgeResult(
        status="AC",
        phase="base",
        detail=f"{sum(1 for r in raw_results if r.status == 'Passed')}/{len(raw_results)} passed",
        runtime_ms=sum(r.runtime_ms for r in raw_results),
    )


def _run_analysis(
    state: SessionState,
    problem_dict: Any,
    code: str,
    raw_results: list,
    deterministic_verdict: str,
) -> JudgeAnalysis:
    """分发判题分析：sample=本地快速反馈（跳过 LLM）；full=LLM 驱动分析。

    两种路径 verdict 都永远以执行引擎客观结果为准，不信任 LLM 主观判断。

    2026-10-25 起只在 **AC** 时被调用（非 AC 的反馈文案归 ``agent_tutor_node``）。

    - 提交(full)：走 LLM 生成 warm_feedback + repair_suggestion（面试官式诊断）。
    - 运行(sample)：跳过 LLM，直接用执行引擎客观结果拼简短诊断反馈——
      运行=快速自测，不该等 LLM（重构前 run.py 纯执行约 1~2s；统一收口后误入 LLM 致 14~85s）。
    """
    if state.judge_scope == "sample":
        passed = sum(1 for r in raw_results if r.status == "Passed")
        total = len(raw_results)
        _fb = (
            f"样例用例 {passed}/{total} 通过 ✅"
            if deterministic_verdict == "AC"
            else f"样例用例 {passed}/{total} 通过，还有问题，继续调试～"
        )
        logger.info("运行(sample) 跳过 LLM 判题分析，verdict=%s", deterministic_verdict)
        return JudgeAnalysis(
            verdict=deterministic_verdict,
            warm_feedback=_fb,
            repair_suggestion="",
        )
    return analyze_judge_results(
        code=code,
        title=problem_dict.title,
        difficulty=problem_dict.difficulty or state.difficulty,
        topic=problem_dict.topic or state.topic,
        description=problem_dict.description,
        results=raw_results,
        forced_verdict=deterministic_verdict,
    )


def _apply_side_effects(
    state: SessionState,
    verdict: str,
    raw_results: list,
    test_cases: list,
    is_run: bool,
    update: dict,
    feedback_msg: str = "",
    code: str = "",
    func_sig: str = "",
) -> None:
    """应用画像写入、sample 诊断与路由 status，原地修改 ``update``。

    - 兼容旧版 v1 全局画像：仅真实提交（非运行）写，直接命中 DB（微决策 1）。
    - sample 诊断：写 last_run_results，样例全过时追加鼓励提交提示。
    - 路由：full+AC→done（交由 update_profile_node 落 v2 画像）；非 AC→tutoring；
      sample+AC→awaiting_submit（不写画像、不 done，保持运行循环）。

    ``verdict`` 是执行引擎的权威结果，不再取自 LLM 的 ``analysis``；
    ``feedback_msg`` 仅在 AC 线有值（非 AC 的文案由 agent_tutor_node 生成）。
    """
    # ── 兼容旧版 v1 全局画像（综合熟练度/做题数等顶部指标）──
    if not is_run:
        try:
            from code_tutor_agent.db.database import update_profile_on_result
            topic = state.problem.topic if state.problem else "未知"
            update_profile_on_result(topic=topic, verdict=verdict,
                                     user_id=getattr(state, "user_id", "default"))
        except Exception:
            logger.warning("Agent v1 profile update failed (non-fatal)", exc_info=True)

        # ── 错误模式画像（error-mode-tracking 特性，fire-and-forget）──
        # 仅真实提交触发；AC 也跑（捕捉"提交前自查修复"的弱项），非 AC 额外给
        # 判题失败补充 feeder ×1.3。分析在守护线程后台进行，不阻塞判题返回。
        try:
            from code_tutor_agent.profile.edit_trace_analyzer import (
                fire_and_forget_error_mode_analysis,
                judge_failure_to_tags,
            )
            prob = state.problem
            if isinstance(prob, dict):
                topic = prob.get("topic", "") or "未知"
                description = prob.get("description", "") or ""
            else:
                topic = getattr(prob, "topic", "") or "未知"
                description = getattr(prob, "description", "") or ""
            judge_tags = None
            if verdict != "AC":
                # 画像用户画像和error_mode 指标
                judge_tags = judge_failure_to_tags(verdict)
            fire_and_forget_error_mode_analysis(
                state.session_id,
                topic=topic,
                description=description,
                final_code=code,
                verdict=verdict,
                judge_failure_tags=judge_tags,
                user_id=getattr(state, "user_id", "default"),
            )
        except Exception:
            logger.warning("error-mode fire-and-forget hook failed (non-fatal)", exc_info=True)

    # ── 运行（sample scope）：写诊断 last_run_results，必要时给轻提示 ──
    if state.judge_scope == "sample":
        update["last_run_results"] = _to_run_results(raw_results, test_cases, func_sig)
        if verdict == "AC":
            # 微决策 3：样例全过 → 鼓励提交完整用例
            hint = "\n\n💡 样例都过了，点「提交」跑完整用例试试吧！"
            update["warm_feedback"] = update.get("warm_feedback", "") + hint
            if feedback_msg:
                update["tutor_messages"] = update["tutor_messages"][:-1] + [
                    {"role": "tutor", "content": feedback_msg + hint}
                ]

    # ── 分 tag 画像（v2）：仅在 full + AC 经 update_profile_node 单写者通道落库 ──
    # 运行（is_run）或 sample 一律不写画像（微决策 1，解读 X）。
    #
    # 【设计 tradeoff：ELO 为什么只在 AC 时更新 ?】
    # 语义：ELO = 最终能力证明，不是逐次计分。AC 前的 WA→辅导→重试是学习过程本身，
    #   惩罚重试＝惩罚学习行为；本产品是 tutoring 而非竞技排名（逐次计分是 Codeforces 式诉求）。
    # 失败信号没有丢，只是分流到别的通道：
    #   - v1 全局画像：每次真实提交都写，WA/TLE/RE 均降 proficiency（db.update_profile_on_result）
    #   - error-mode 画像：每次真实提交都跑（见下方 fire-and-forget 块），失败额外加权
    #   - 编辑轨迹：过程挣扎走 edit_trace 通道
    # 不会刷分通胀：期望分项 E 使低于自身水平的题 AC 增益趋近 0，重复刷简单题自动饱和。
    #
    # 已知边界（有意接受）：
    #   - stab/attempts 与 ELO 同门控：stab 窗口恒 [1,...]、方差恒 0，「稳定性」维度退化；
    #   - tag 从未 AC 则恒 1500 初始分，planner 分不清「没练过」vs「练了不行」
    #     （便宜的补法：planner 消费 errors/attempts，不动 ELO）。
    # 若未来要「失败也更新 ELO」：本分支对非 AC full 也挂 delta（outcome=真实 verdict；注意
    #   "CE" 不在 ProfileDelta.outcome 的 Literal 内，需扩 schema）+ graph.py 路由改为
    #   非 AC full → update_profile_node、静态边 update_profile_node → critic_node 改条件边
    #   按 status 分流；生产代码约 25 行，需同步改 wiring smoke / judge 节点测试断言。
    #   scoring.apply_delta 的 S=0 分支零改动（S=0 → ELO 自然下降、stab 记 0）。
    if verdict == "AC" and not is_run and state.judge_scope != "sample":
        update["profile_delta"] = {
            "tag_primary": state.problem.tag_primary,
            "prob_elo": state.problem.prob_elo,
            "outcome": verdict,
            "fingerprints": [],
        }
        # AC 收尾状态在这里一次写齐：后续链路为 update_profile_node → critic_node
        # （AC 分支：flush problem_history + phase=reviewing + 暂停在 wait_for_submit）。
        # 与常规 judge 保持一致：只有最终 AC 才记录（WA 重试不写画像，tradeoff 见上）。
        update["status"] = "done"
        logger.info("full+AC — routing to update_profile_node → critic_node")
    elif verdict != "AC":
        # ── WA：直接回 agent_tutor_node 给重试指导（不写画像，文案也在那里生成）──
        update["status"] = "tutoring"
        logger.info("Not AC — routing to agent_tutor_node for retry guidance")
    else:
        # ── sample + AC（运行）：不写画像、不 done，回 wait_for_submit 保持循环 ──
        update["status"] = "awaiting_submit"
        logger.info("sample+AC (运行) — routing to wait_for_submit_node (no profile, no done)")


def agent_judge_node(state: SessionState) -> dict:
    """LangGraph node: execute the user's code and record the authoritative verdict.

    Reads ``state.judge_scope`` ('sample' for 运行, 'full' for 提交) to pick
    visible vs full test cases. On sample scope, writes diagnostic
    ``last_run_results`` only — never writes profile. Non-AC 文案不在此生成，
    改为写 ``last_exec_results`` 交棒给 ``agent_tutor_node``；AC 线的反馈仍在
    本节点生成（AC 分支在图上不经辅导节点）。Routes via the graph conditional
    edge ``agent_judge_router``:
      - WA                 → agent_tutor_node（交棒客观结果）
      - full + AC          → update_profile_node (writes v2 profile, then done)
      - sample + AC / 运行 → wait_for_submit_node (no profile, no done)

    Args:
        state: Session state with submissions, problem, and judge_scope.

    Returns:
        Plain state update dict (no Command); routing handled by the graph.
    """
    logger.info("▶ agent_judge_node() — cycle=%d scope=%s", state.judge_cycle + 1, state.judge_scope)

    # ── 校验输入 + 加载 problem/test_cases（任一前置不满足返回错误 update）──
    try:
        resolved = _resolve_inputs(state)
    except Exception as exc:  # noqa: BLE001
        # 基础设施抖动（典型：SQLite 并发写锁导致 get_problem_by_id 抛错）不应击穿节点。
        # 抛异常会让 /submit 直接 500、状态不落盘、会话失去挂起节点，
        # 此后 /run 一律 400「当前不可运行」——整道题永久判不了（2026-09-04 回归实测）。
        # 返回 status=error 由 agent_judge_router 路由到 wait_for_submit_node 保活，
        # 用户改代码再提交即可重试。
        logger.exception("agent_judge_node — 前置加载失败，会话保活: %s", exc)
        _m_judge_error(f"前置加载失败：{exc}")
        return {"status": "error", "error_message": f"判题前置加载失败：{exc}"}
    if isinstance(resolved, dict):
        return resolved
    code, is_run, problem_dict, test_cases = resolved

    # ── Run test cases via Judge0 (or local fallback) ──
    func_sig = _resolve_function_signature(problem_dict)
    raw_results = run_solution(code, test_cases, timeout=AGENT_JUDGE_TIMEOUT, function_signature=func_sig)
    logger.info("Judge0 returned %d results", len(raw_results))

    # ── 权威 verdict：永远以执行引擎客观结果为准（不信任 LLM 的主观判断） ──
    deterministic_verdict = _deterministic_verdict(raw_results)

    # 仅真实「提交」(is_run=False) 才写 judge_results；运行(is_run=True) 只更新
    # last_run_results（下方 _apply_side_effects）。否则运行结果会被 chat 误读成「提交判题 AC」，
    # 导致用户只点了运行、未提交，却收到「恭喜你 AC」的误导（2026-08-13 实测）。
    # 注意：submissions 通道是 merge_submissions reducer（按 index 替换）——变更
    # 提交后必须显式返回，PostgresSaver 不持久化对通道对象的 in-place 修改
    # （get_state 从 checkpoint 重建，2026-09-07 实测 judge_results 全丢）。
    submissions_update: list = []
    if state.submissions and not is_run:
        state.submissions[-1].judge_results.append(_build_base_result(raw_results, test_cases, func_sig))
        submissions_update = [state.submissions[-1]]

    # ── 判题分析：仅 AC 线在本节点出文案（祝贺 + 复杂度/优化建议）──
    # 非 AC 不再调 LLM 生成反馈：那是辅导节点的职责，判题只把客观结果交棒过去。
    feedback_msg = ""
    update: dict[str, Any] = {
        "last_verdict": deterministic_verdict,
        "judge_cycle": state.judge_cycle + 1,
        # 交棒载荷：agent_tutor_node 读它生成辅导文案（不重跑用例、不改判 verdict）
        "last_exec_results": _to_plain_results(raw_results),
    }
    if deterministic_verdict == "AC":
        if state.tutor_deferred and state.judge_scope != "sample":
            # /submit 拆链路：AC 的「祝贺 + 复杂度/优化建议」也交后台补写
            # （与辅导节点共用 build_tutor_update）。请求里只跑执行引擎拿 verdict。
            # 显式清空陈旧文案，否则前端会在文案到达前读到上一轮的点评。
            # sample（运行）不延迟：它的反馈是本地拼的、不花钱。
            logger.info("Verdict: AC — 文案延迟后台生成（defer）")
            update["warm_feedback"] = ""
            update["repair_suggestion"] = ""
        else:
            analysis = _run_analysis(state, problem_dict, code, raw_results, deterministic_verdict)
            logger.info("Verdict: %s (AC 线在本节点生成反馈)", deterministic_verdict)
            feedback_msg = analysis.warm_feedback
            if analysis.repair_suggestion:
                feedback_msg += f"\n\n**修复建议**\n{analysis.repair_suggestion}"
            update["warm_feedback"] = analysis.warm_feedback
            update["repair_suggestion"] = analysis.repair_suggestion
            update["tutor_messages"] = state.tutor_messages + [
                {"role": "tutor", "content": feedback_msg}
            ]
    else:
        logger.info("Verdict: %s — 文案交棒 agent_tutor_node", deterministic_verdict)
    _m_judge_ok()

    # ── Update state ──
    # submissions 用 merge_submissions reducer（按 index 替换）：只返回变更的那一条，
    # 未变更时不带该 key（不要返回全量列表以外的写法，index 重复会被覆盖）。
    if submissions_update:
        update["submissions"] = submissions_update

    _apply_side_effects(state, deterministic_verdict, raw_results, test_cases, is_run, update, feedback_msg, code=code, func_sig=func_sig)
    return update
