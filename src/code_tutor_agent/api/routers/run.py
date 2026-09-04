"""Run router — POST /session/{sid}/run.

统一走 graph：经 wait_for_submit 的 interrupt resume 注入 scope="sample"，
复用提交判题链路（agent_judge_node 跑可见用例），落库诊断 last_run_results，
再从 state 重建 RunCodeResponse（保持前端契约不变）。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from langgraph.types import Command

from code_tutor_agent.api.deps import get_graph
from code_tutor_agent.observability import build_run_config
from code_tutor_agent.schemas.api import RunCodeRequest, RunCodeResponse
from code_tutor_agent.schemas.state import SessionState

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/{sid}/run", response_model=RunCodeResponse)
async def run_code(sid: str, body: RunCodeRequest):
    """Run the user's code against visible (sample) test cases via the graph."""
    graph = get_graph()
    config = build_run_config(sid, run_name="run_code")

    try:
        state = graph.get_state(config)
    except Exception:
        raise HTTPException(404, f"Session {sid} not found")

    # ── 卡死兜底：会话停在终态但题已就绪（无挂起节点）时重新挂到 wait_for_submit ──
    # 两种历史卡死：
    #   1) dialog + problem：旧会话 graph 停在 END、无挂起中断，resume 空转；
    #   2) error + problem：判题前置失败（如无测试用例）把 status 置 error 并走到 END，
    #      此后 /run 一律 400、/submit 空转返回开场白（题目 124 事故）。
    # 二者症状相同（next=()），统一处理：清掉错误态并带输入重跑 graph，
    # 让它真正暂停到 wait_for_submit_node 的 interrupt，再走下方正常判题。
    if (
        state.values.get("status") in ("dialog", "error")
        and state.values.get("problem")
        and "wait_for_submit_node" not in (state.next or ())
    ):
        logger.warning(
            "run: session %s stuck in status=%s with problem — forwarding to wait_for_submit",
            sid, state.values.get("status"),
        )
        # 注意：invoke(None) 在 checkpoint next=()（无挂起任务）时是空操作，
        # 不会重跑 graph——必须带输入重跑。输入来自旧 checkpoint 会覆盖
        # update_state 的值，所以 complete 要直接写进输入，才会从 __start__ 经
        # agent_dialog_node(complete) → planner_node(problem 已加载) → wait_for_submit 暂停。
        _resume_input = SessionState(**state.values).model_dump()
        _resume_input["agent_dialog_complete"] = True
        _resume_input["status"] = "awaiting_submit"
        _resume_input["error_message"] = ""
        await asyncio.to_thread(graph.invoke, _resume_input, config)
        state = graph.get_state(config)

    # 只有 graph 暂停在 wait_for_submit（等待提交/运行）时才允许运行；
    # 出题中、对话中、已结束等状态不允许，避免打断生成或误触发判题。
    if "wait_for_submit_node" not in (state.next or ()):
        raise HTTPException(400, "当前不可运行：会话未在等待提交（可能正在出题或已结束）")

    problem = state.values.get("problem")
    if not problem:
        raise HTTPException(400, "No problem loaded in this session")

    # 上一次判题失败遗留的错误态（router 已把会话保活回 wait_for_submit）。
    # 不清会一直挂在 state 上，前端 /state 与后续判断都可能误读；
    # 用 pause_safe_update 写入，避免在 interrupt 挂起期丢掉挂起中断。
    if state.values.get("status") == "error":
        from code_tutor_agent.api.deps import pause_safe_update

        pause_safe_update(graph, config, {"status": "awaiting_submit", "error_message": ""})

    def _do_run() -> None:
        # 判题是同步阻塞的（graph.invoke 内含 LLM 调用），丢进线程池执行，
        # 避免独占事件循环、拖垮同进程的其它请求与 SSE 推流。
        graph.invoke(
            Command(resume={"code": body.code, "language": body.language, "scope": "sample"}),
            config,
        )

    await asyncio.to_thread(_do_run)
    state = graph.get_state(config)
    values = state.values

    run_results = values.get("last_run_results") or []
    all_passed = all(r.get("passed") for r in run_results) if run_results else False
    return RunCodeResponse(
        session_id=sid,
        all_passed=all_passed,
        results=run_results,
        total=len(run_results),
        passed=sum(1 for r in run_results if r.get("passed")),
    )
