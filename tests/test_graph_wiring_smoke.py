"""图连线 / 节点路由 离线冒烟测试（无 LLM、无 DB）。

为什么需要这一套
----------------
最近几轮 bug 都属于「图内部连线 / 节点路由」类错误，单个函数单测发现不了：

* 2026-07-21  `profile/node.py` 误写 ``from langgraph.graph import Command``
  （正确是 ``langgraph.types``）→ /submit 走到 update_profile_node 即 ImportError 崩溃。
* 同轮  update_profile_node 返回 ``Command(goto="critic_node")`` 与 graph.py 已有
  静态边 ``update_profile_node → critic_node`` 冲突。
* multi-question 分支：SessionPhase 缺 ``dialog`` 值 → graph.invoke Pydantic 校验失败。
* 更早：analyze_user_intent 把 dict 当 Message 读 ``.role`` → AttributeError。

这些只有「编译整张图」或「直接跑节点看它返回什么路由」才拦得住。
``compile_graph()`` 只在服务启动跑一次，所以函数层改动不会触发——本文件补上这条防线。

2026-08-04 更新：经 scripts/verify_command_edge_conflict.py 实测确认，langgraph 1.2.7
中 Command(goto) **不会覆盖**静态边（两者同时生效）。据此规则：返回 Command(goto) 的
节点一律不加静态出边，否则会双节点执行。

2026-08-13 agent-only 重构：normal 模式节点（judge_node / tutor_node /
constitutional_guard_node）与 sandbox/adversarial.py 全部删除，判题统一走
agent_judge_node。agent_judge_node / agent_tutor_node / update_profile_node 改为
返回纯 dict，路由改由图的条件边 / 静态边承担：
  - agent_judge_node → 条件边 agent_judge_router（error/AC/WA 分支）
  - update_profile_node → 静态边 critic_node（确定性单出口）
  - agent_tutor_node → 静态边 wait_for_submit_node（确定性单出口）
planner_node / generator_node / agent_dialog_node / critic_node 保留 Command(goto)
（含分支/暂停/错误路径，改静态边会与错误/分支 Command 冲突）。

运行:  uv run pytest tests/test_graph_wiring_smoke.py -q
"""
from __future__ import annotations

from typing import Annotated

from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from pydantic import BaseModel, Field

from code_tutor_agent.graph.graph import _build_graph, compile_graph
from code_tutor_agent.profile.node import update_profile_node
from code_tutor_agent.schemas.state import SessionPhase, SessionState, last_phase, last_wins_list


def _make_state(mode: str) -> SessionState:
    """构造一个最小可用的 SessionState；profile_delta 给真值以触发路由分支。

    （update_profile_node 在 profile_delta 为空时直接 return {}，走不到路由逻辑。）
    """
    return SessionState(
        session_id="smoke-session",
        mode=mode,  # type: ignore[arg-type]
        profile_delta={
            "tag_primary": "array_two_pointers",
            "prob_elo": 1500,
            "outcome": "AC",
            "fingerprints": [],
            "misunderstanding_level": None,
        },
    )


def _invoke_update_profile(state: SessionState):
    store = InMemoryStore()
    config = {"configurable": {"user_id": "default"}}
    # 节点内部会在 profile_delta 非空时写 SQLite（save_user_profile_v2），
    # 该调用被 try/except 包裹，离线无 DB 时仅告警、不影响路由返回，故可安全离线跑。
    return update_profile_node(state, store=store, config=config)


def test_graph_compiles():
    """编译整张图失败 = 某节点 top-level 导入错误 / 边冲突 / 节点未注册。

    这能在 import 阶段抓出 ``from langgraph.graph import Command`` 这类错误
    （凡在模块顶层 import 的节点都会在此被加载）。
    """
    g = compile_graph()
    assert g is not None


def test_all_nodes_importable():
    """逐一 import 每个 node / profile 模块，确保顶层 import 路径正确。

    profile.node 的 Command 路由（两种模式都 goto critic_node）由
    test_update_profile_node_routes_to_critic 验证。
    """
    import importlib

    modules = [
        "code_tutor_agent.nodes.generator",
        "code_tutor_agent.nodes.planner",
        "code_tutor_agent.nodes.critic",
        "code_tutor_agent.nodes.wait_for_submit",
        "code_tutor_agent.profile.node",
        "code_tutor_agent.nodes.agent_dialog",
        "code_tutor_agent.nodes.agent_judge",
        "code_tutor_agent.nodes.agent_tutor",
    ]
    for mod in modules:
        importlib.import_module(mod)


def test_update_profile_node_returns_plain_dict():
    """update_profile_node 返回纯 dict（非 Command）；路由由图静态边承担。

    agent-only 重构（2026-08-13）：节点不再 return Command(goto="critic_node")，
    改为返回 ``{}``，由 graph 静态边 ``update_profile_node → critic_node`` 路由。
    这样确定性单出口节点与 Command 节点解耦，避免 2026-08-04 的双执行坑。
    """
    for mode in ("practice", "agent"):
        state = _make_state(mode)
        result = _invoke_update_profile(state)
        assert isinstance(result, dict), f"{mode} 模式应返回 dict，实际: {result!r}"
        assert "goto" not in result, "不应再含 goto（改由静态边路由）"


def test_update_profile_static_edge_routes_to_critic():
    """编译后的图必须含静态边 update_profile_node → critic_node（链路不断）。

    agent-only 重构（2026-08-13）：节点不再 return Command(goto)，改由图静态边路由。
    这里直接 inspect builder 的静态边列表验证（langgraph 1.2.x 的
    ``CompiledStateGraph.get_graph().edges`` 会把图折叠成 ``__start__→__end__``，
    无法反映内部静态边，故改用未编译的 ``_build_graph().edges``，其为
    ``(source, target)`` 元组列表）。
    """
    b = _build_graph()
    edges = [tuple(e) for e in b.edges]
    assert ("update_profile_node", "critic_node") in edges


def test_agent_tutor_static_edge_routes_to_wait_for_submit():
    """编译后的图必须含静态边 agent_tutor_node → wait_for_submit_node。"""
    b = _build_graph()
    edges = [tuple(e) for e in b.edges]
    assert ("agent_tutor_node", "wait_for_submit_node") in edges


def test_agent_only_node_set():
    """agent-only 重构后，编译图应只含 9 个节点（含 __start__）；

    normal 模式节点 judge_node / tutor_node / constitutional_guard_node 必须彻底消失。
    """
    g = compile_graph()
    expected = {
        "__start__",
        "agent_dialog_node",
        "agent_judge_node",
        "agent_tutor_node",
        "critic_node",
        "generator_node",
        "planner_node",
        "update_profile_node",
        "wait_for_submit_node",
    }
    actual = set(g.nodes.keys())
    assert actual == expected, f"节点集不符:\n  期望={expected}\n  实际={actual}"
    for gone in ("judge_node", "tutor_node", "constitutional_guard_node"):
        assert gone not in actual, f"已删除节点不应残留: {gone}"


def test_session_phase_dialog_exists():
    """锁定 multi-question 修复：SessionPhase 必须含 dialog 值，否则 /submit 的
    next_problem 写 ``phase: dialog`` 会让 graph.invoke Pydantic 校验失败。
    """
    assert SessionPhase("dialog") == SessionPhase.dialog


def test_last_phase_reducer_takes_last():
    """last_phase reducer：同拍多写时取最后一个；单写原样返回。"""
    assert last_phase(SessionPhase.solving, SessionPhase.reviewing) == SessionPhase.reviewing
    assert (
        last_phase(SessionPhase.solving, [SessionPhase.reviewing, SessionPhase.done])
        == SessionPhase.done
    )


def test_phase_channel_tolerates_multiple_writes_per_step():
    """回归 2026-07-21 /submit 崩溃：

    'At key phase: Can receive only one value per step.
     Use an Annotated key to handle multiple values.'

    根因：phase 被多个 node 在不同图步写，某些多轮状态下两个写者落进
    同一图步，默认 last_value 通道直接抛错。phase 已改为
    ``Annotated[SessionPhase, last_phase]``（同拍多写取最后一个）。

    本测试构建一个最小图：START 并行指向 a、b 两个节点，二者在**同一拍**
    都写 phase，验证通道不再抛 InvalidUpdateError，且取二者之一。
    """
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from pydantic import BaseModel

    class _Mini(BaseModel):
        phase: Annotated[SessionPhase, last_phase] = SessionPhase.solving

    def _a(state):
        return {"phase": SessionPhase.reviewing}

    def _b(state):
        return {"phase": SessionPhase.done}

    g = StateGraph(_Mini)
    g.add_node("a", _a)
    g.add_node("b", _b)
    g.add_edge(START, "a")
    g.add_edge(START, "b")  # 并行：a、b 同拍都写 phase
    g.add_edge("a", END)
    g.add_edge("b", END)
    app = g.compile(checkpointer=InMemorySaver())

    out = app.invoke(
        {"phase": SessionPhase.solving},
        {"configurable": {"thread_id": "mini-phase"}},
    )
    assert out["phase"] in (SessionPhase.reviewing, SessionPhase.done)


def test_last_wins_list_reducer_takes_last():
    """last_wins_list reducer：多写取最后一个；单写原样返回；空列表可清空。"""
    assert last_wins_list(["a"], ["a", "b"]) == ["a", "b"]
    assert last_wins_list(["a", "b"], []) == []          # critic 清空 tutor_messages
    assert last_wins_list(["a"], ["a", "u1", "t1"]) == ["a", "u1", "t1"]


def test_last_wins_list_tolerates_repeated_pause_safe_writes():
    """回归 2026-08-07 连续点「运行」报错：

    'At key tutor_messages: Can receive only one value per step.
     Use an Annotated key to handle multiple values.'

    根因：tutor_messages 无 reducer（LastValue 单值通道），而 pause_safe_update 在
    graph 暂停于 wait_for_submit_node 时用 ``invoke(Command(update=..., goto=...))``
    重装中断，暂停期**第二次**写入同一单值通道即触发 langgraph 同一步双写校验；
    与是否并发无关（纯串行实验第二次必炸）。

    修复：tutor_messages / agent_dialog_history / problem_history 改为
    ``Annotated[list, last_wins_list]``（last-wins，非 operator.add——所有写入者
    传全量列表，add 会导致历史重复追加）。

    本测试验证：暂停在 interrupt 上、连续多次 ``invoke(Command(update, goto))``
    写同一 list 通道不再抛 InvalidUpdateError，且消息不丢不重。
    """
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import StateGraph
    from langgraph.types import interrupt

    class _Mini(BaseModel):
        tutor_messages: Annotated[list[str], last_wins_list] = Field(default_factory=list)
        status: str = ""

    def _wait(state):
        interrupt({"type": "awaiting_submit"})
        return {}

    def _router(state):
        return "wait"

    g = StateGraph(_Mini)
    g.add_node("wait", _wait)
    g.add_conditional_edges("__start__", _router)
    app = g.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "mini-tm"}}

    app.invoke({"tutor_messages": [], "status": "awaiting_submit"}, config)
    assert app.get_state(config).next == ("wait",)

    # 模拟 run→chat 保存连续 3 轮（真实时序：每轮基于当前快照 + 追加新消息）
    for i in range(3):
        cur = app.get_state(config).values.get("tutor_messages", [])
        app.invoke(
            Command(update={"tutor_messages": list(cur) + [f"u{i}", f"t{i}"]}, goto="wait"),
            config,
        )

    final = app.get_state(config).values.get("tutor_messages", [])
    assert final == ["u0", "t0", "u1", "t1", "u2", "t2"], f"消息丢失/重复: {final}"
