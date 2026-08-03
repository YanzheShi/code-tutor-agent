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
中 Command(goto) **不会覆盖**静态边（两者同时生效）。据此移除了
judge→tutor_router / agent_judge→agent_tutor / update_profile→critic 三条静态边，
update_profile_node 改为统一返回 Command(goto="critic_node")，相应测试见下方。

运行:  uv run pytest tests/test_graph_wiring_smoke.py -q
"""
from __future__ import annotations

from typing import Annotated

from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from code_tutor_agent.graph.graph import compile_graph
from code_tutor_agent.profile.node import update_profile_node
from code_tutor_agent.schemas.state import SessionPhase, SessionState, last_phase


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
        "code_tutor_agent.nodes.judge",
        "code_tutor_agent.nodes.planner",
        "code_tutor_agent.nodes.tutor",
        "code_tutor_agent.nodes.tutor_router",
        "code_tutor_agent.nodes.critic",
        "code_tutor_agent.nodes.wait_for_submit",
        "code_tutor_agent.profile.node",
        "code_tutor_agent.nodes.agent_dialog",
        "code_tutor_agent.nodes.agent_judge",
        "code_tutor_agent.nodes.agent_tutor",
        "code_tutor_agent.nodes.chat",
        "code_tutor_agent.nodes.constitutional_guard",
    ]
    for mod in modules:
        importlib.import_module(mod)


def test_update_profile_node_routes_to_critic():
    """两种模式统一返回 Command(goto="critic_node")。

    回归点（2026-08-04）：langgraph 1.2.7 中 Command(goto) 不会覆盖静态边，
    原「静态边 → critic_node + agent 模式 Command(goto=agent_tutor_node)」组合
    导致 critic 与 agent_tutor 并行双执行。静态边已移除，路由统一走 Command。
    同时覆盖 ``from langgraph.types import Command`` 的正确导入路径。
    """
    for mode in ("practice", "agent"):
        state = _make_state(mode)
        result = _invoke_update_profile(state)
        assert isinstance(result, Command), f"{mode} 模式应返回 Command，实际: {result!r}"
        assert result.goto == "critic_node", (
            f"{mode} 模式应路由到 critic_node，实际 goto={getattr(result, 'goto', None)!r}"
        )


def test_update_profile_node_routes_even_without_delta():
    """profile_delta 为空时跳过写库，但仍必须路由到 critic_node（链路不断）。"""
    state = SessionState(session_id="smoke-session", mode="practice")
    result = _invoke_update_profile(state)
    assert isinstance(result, Command)
    assert result.goto == "critic_node"


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
    from pydantic import BaseModel
    from langgraph.graph import END, START, StateGraph
    from langgraph.checkpoint.memory import InMemorySaver

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
