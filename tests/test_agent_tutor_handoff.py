"""判题→辅导 交棒链路的离线图级测试（无 LLM、无 DB、无 PG）。

为什么需要这一套
----------------
2026-10-25 职责归位把「非 AC 的辅导文案」从 `agent_judge_node` 迁到了
`agent_tutor_node`。单节点单测各自成立，但**跨节点的两个坑只有跑真实编译图才拦得住**：

1. **气泡双写**：若有人在判题侧把文案加回来，一轮 WA 会吐出两条导师气泡
   （tutor_messages 走 last_wins_list，不报错，只是历史里多一条 —— 前端表现为复读）。
2. **交棒断链**：`last_exec_results` 是判题写、辅导读的隐式契约。改名/漏写/序列化
   失真（例如 expected/actual 丢失）都会让辅导退化成「凭代码瞎猜」，而节点级 mock 测
   试两边各测各的，谁都发现不了。

同时锁一条事故回归：一轮判题跑完，会话必须仍挂在 `wait_for_submit_node` 上
（否则 /run 400、/submit 空转 —— 见 2026-10-01 与题目 124 事故）。

运行:  uv run pytest tests/test_agent_tutor_handoff.py -q
"""
from __future__ import annotations

from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from code_tutor_agent.db.models import DBProblem
from code_tutor_agent.graph.graph import _build_graph
from code_tutor_agent.sandbox.runner import RunnerResult
from code_tutor_agent.schemas.state import ProblemMeta

_BAD_CODE = "class Solution:\n    def twoSum(self, nums, target):\n        return []"

_WA_RESULTS = [
    RunnerResult(0, "Wrong Answer", "expected=[0,1] got=[-1,-1]", runtime_ms=4.0,
                 expected_output="[0,1]", actual_output="[-1,-1]"),
    RunnerResult(1, "Passed", "", runtime_ms=2.0,
                 expected_output="[1,2]", actual_output="[1,2]"),
]


def _problem() -> DBProblem:
    return DBProblem(
        id=1,
        title="两数之和",
        topic="数组",
        difficulty="easy",
        description="给定数组与目标值，返回和为目标的两个下标。",
        test_cases_json='[{"input_args": ["[2,7,11,15]", "9"], "expected_output": "[0,1]"}]',
    )


def _meta() -> ProblemMeta:
    return ProblemMeta(
        problem_id=1,
        title="两数之和",
        topic="数组",
        difficulty="easy",
        description="给定数组与目标值，返回和为目标的两个下标。",
    )


def _armed_session(thread_id: str):
    """造一个真实挂在 wait_for_submit **interrupt** 上的会话（离线、零 LLM）。

    为什么不能用 `update_state` 把 next 直接摆成 (wait_for_submit_node,)：那只是
    「路由指针」，没有挂起任务，`Command(resume=...)` 找不到 interrupt 会空转
    （graph.py 里题目 124 的注释描述的就是这个形态）。必须让 `interrupt()` 真被
    执行过一次，resume 才恢复得起来。

    也不能走真实出题链来 arm：`generator_node` 没有「已有题就跳过」的短路，必然
    真调 LLM 出题（实测约 3 分钟）。所以装配时把与交棒契约无关的两个出题侧节点
    （planner / generator）换成直通桩 —— **被测链路（wait_for_submit → judge →
    agent_judge_router → tutor → 静态边回 wait）全部是生产实现**。

    ⚠️ 顺带实测到一条 langgraph 语义（原写法踩坑）：在**空 thread** 上
    `invoke(Command(update=..., goto=...))` 不会只跑 goto —— 它会先从 `__start__`
    初始化并连带执行 `start_router`，把 `agent_dialog_node` 真跑一遍（真调 LLM 发
    开场白），该节点与 input 写在**同一拍**都碰 `status` → InvalidUpdateError:
    At key 'status'。与 2026-10-01「/run 永久 400」事故里「写状态顺带跑了 router」
    是同一机制；生产侧由 test_write_as_node_choices_are_wired_as_documented 守着，
    本函数守的是测试侧。
    """
    def _stub_planner(state):
        return Command(goto="generator_node")

    def _stub_generator(state):
        return Command(goto="wait_for_submit_node")

    with (
        patch("code_tutor_agent.graph.graph.planner_node", _stub_planner),
        patch("code_tutor_agent.graph.graph.generator_node", _stub_generator),
    ):
        graph = _build_graph().compile(checkpointer=InMemorySaver())

    cfg = {"configurable": {"thread_id": thread_id}}
    graph.invoke(
        {
            "session_id": thread_id,
            "mode": "agent",
            "status": "awaiting_submit",
            "agent_dialog_complete": True,   # 跳过出题对话（否则 dialog 会真调 LLM）
            "topic": "数组",
            "difficulty": "easy",
            "problem": _meta(),
        },
        cfg,
    )

    state = graph.get_state(cfg)
    assert "wait_for_submit_node" in (state.next or ()), f"未挂上中断: next={state.next}"
    # 结构化守卫：出题对话节点一旦被误执行，就会真调 LLM 写开场白 —— 这里立刻失败，
    # 而不是静默烧网关、把交棒链路测成别的东西。
    assert not state.values.get("agent_dialog_history"), (
        "agent_dialog_node 被执行了：arming 走成了真实出题链，本测试会真调 LLM"
    )
    return graph, cfg


def _submit_wa(graph, cfg):
    """提交一份错代码，走真实图：wait(resume) → judge → router → tutor → wait。

    只 patch 外部边界：沙箱执行、LLM 文案，以及与交棒契约无关的画像副作用
    （v1 画像直写 + error-mode 后台线程）——不 patch 它们会把行写进开发 PG，
    还会起守护线程，与本测试要验的东西无关。路由、状态通道、checkpointer 全真。
    """
    with (
        patch("code_tutor_agent.nodes.agent_judge.get_problem_by_id", return_value=_problem()),
        patch("code_tutor_agent.nodes.agent_judge.run_solution", return_value=_WA_RESULTS),
        patch("code_tutor_agent.nodes.agent_judge.analyze_judge_results") as m_judge_llm,
        patch("code_tutor_agent.nodes.agent_tutor.analyze_judge_results") as m_tutor_llm,
        patch("code_tutor_agent.db.database.update_profile_on_result"),
        patch(
            "code_tutor_agent.profile.edit_trace_analyzer.fire_and_forget_error_mode_analysis"
        ),
    ):
        from code_tutor_agent.agents.agent_judge import JudgeAnalysis

        m_tutor_llm.return_value = JudgeAnalysis(
            verdict="WA",
            warm_feedback="第二个用例已经对了，第一个返回了空数组。",
            repair_suggestion="想想目标值与每个元素的差值该怎么配对",
            should_retry=True,
        )
        graph.invoke(Command(resume={"code": _BAD_CODE, "scope": "full"}), cfg)
    return m_judge_llm, m_tutor_llm


def test_wa_cycle_emits_exactly_one_bubble_from_tutor_node():
    """一轮 WA 只产一条导师气泡，且由辅导节点（不是判题节点）生成。"""
    graph, cfg = _armed_session("handoff-one-bubble")

    m_judge_llm, m_tutor_llm = _submit_wa(graph, cfg)
    values = graph.get_state(cfg).values

    m_judge_llm.assert_not_called()
    assert m_tutor_llm.call_count == 1, "辅导文案必须且只能由 agent_tutor_node 生成一次"

    bubbles = [
        m for m in values["tutor_messages"]
        if getattr(m, "role", None) == "tutor" or m.get("role") == "tutor"
    ]
    assert len(bubbles) == 1, f"一轮判题应只有 1 条导师气泡，实际 {len(bubbles)} 条（判题侧把文案加回来了？）"
    text = getattr(bubbles[0], "content", None) or bubbles[0]["content"]
    assert "空数组" in text and "**修复建议**" in text

    assert values["warm_feedback"] == "第二个用例已经对了，第一个返回了空数组。"
    assert values["repair_suggestion"] == "想想目标值与每个元素的差值该怎么配对"


def test_handoff_payload_keeps_every_case_with_expected_and_actual():
    """交棒载荷完整性：辅导侧 LLM 必须看到**每个**用例的期望/实际输出。

    缺 expected/actual 时模型会臆造「你的输出」（评测口径里的幻觉来源），
    只传首个失败用例也不足以定位边界差异。
    """
    graph, cfg = _armed_session("handoff-payload")

    _, m_tutor_llm = _submit_wa(graph, cfg)
    kwargs = m_tutor_llm.call_args.kwargs
    handed = kwargs["results"]

    assert len(handed) == len(_WA_RESULTS), "逐用例结果必须全量交棒，不能只给失败的那条"
    assert [r.status for r in handed] == ["Wrong Answer", "Passed"]
    assert handed[0].expected_output == "[0,1]" and handed[0].actual_output == "[-1,-1]"
    # verdict 权威属于判题：辅导只能把它当 forced_verdict 传入，不得改判
    assert kwargs["forced_verdict"] == "WA"
    assert graph.get_state(cfg).values["last_verdict"] == "WA"


def test_wa_cycle_leaves_session_armed_for_next_run_or_submit():
    """判题+辅导跑完，会话必须仍真实挂在 wait_for_submit 上（可继续 /run、/submit）。

    辅导节点抛异常或路由走偏都会让 next 变空 → /run 400、/submit 空转（历史事故形态）。
    """
    graph, cfg = _armed_session("handoff-rearm")

    _submit_wa(graph, cfg)
    state = graph.get_state(cfg)

    assert "wait_for_submit_node" in (state.next or ()), f"next={state.next}"
    assert state.values["status"] == "awaiting_submit"
    assert state.values["judge_cycle"] == 1
    assert len(state.values["submissions"]) == 1
    # 交棒载荷已落库（下一步重提交会整体替换，不留旧数据）
    assert state.values["last_exec_results"][0]["test_case_id"] == 0

    # 第二次提交再走一遍：气泡累计 2 条、cycle=2，链路不衰减
    _submit_wa(graph, cfg)
    values = graph.get_state(cfg).values
    assert values["judge_cycle"] == 2
    assert "wait_for_submit_node" in (graph.get_state(cfg).next or ())
    assert len([m for m in values["tutor_messages"]]) == 2
