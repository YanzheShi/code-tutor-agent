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

from code_tutor_agent.api.deps import (
    AS_NODE_AWAITING_SUBMIT,
    AS_NODE_NEUTRAL,
    pause_safe_update,
    write_as_node,
)
from code_tutor_agent.api.services.generation import mirror_state
from code_tutor_agent.graph.graph import _build_graph, compile_graph
from code_tutor_agent.profile.node import update_profile_node
from code_tutor_agent.schemas.state import (
    ProblemMeta,
    SessionPhase,
    SessionState,
    last_phase,
    last_wins_list,
)


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


# ─────────────────────────────────────────────────────────────────────────────
# 2026-10-01 做题界面「运行」永久 400 事故
#
# 症状：出题成功后点「运行」→ `POST /run` 400「当前不可运行：会话未在等待提交」，
#       重试、改代码都无效；只要中途**发过一条对话消息**就必然中招。
#
# 根因链（本地纯 langgraph 复现确认）：
#   1. 出题结果靠 `update_state(..., as_node="generator_node")` 从 scratch 线程
#      镜像回真实会话 —— generator_node 只有 Command(goto) 出边，推不出 next →
#      会话停在 `next=()`；且这是真实 thread 的**第一个** checkpoint，
#      `versions_seen` 里只有空记录。
#   2. 做题界面发一条对话 → `pause_safe_update` 见 next 为空 → 走 update_state
#      分支；旧代码**省略 as_node**，langgraph 在 versions_seen 全空时把它推断成
#      `self.input_channels == "__start__"`。
#   3. `__start__` 的 writer 就是 `start_router` 那条条件边 → 写状态顺带执行了
#      router，把 `branch:to:agent_dialog_node` 写进 checkpoint → `next` 被钉成
#      `('agent_dialog_node',)`：既不含 wait_for_submit_node（/run 400），又不为空
#      （run.py / session.py 的卡死兜底判据 `not state.next` 失效）→ 永久卡死。
#
# 本组用例把「写状态之后 next 必须仍然指向 wait_for_submit_node」钉死，
# 全部离线可跑（InMemorySaver，无 LLM、无 DB）。
# ─────────────────────────────────────────────────────────────────────────────


def _problem_meta() -> ProblemMeta:
    return ProblemMeta(
        problem_id=1,
        title="两数之和",
        topic="数组",
        difficulty="easy",
        description="给定数组与目标值，返回和为目标的两个下标。",
    )


def _degraded_session(thread_id: str, *, with_problem: bool):
    """造出事故里的**退化会话**：真实 thread 的首个 checkpoint 由镜像注入产生。

    （真实 thread 从没真正跑过图 → `next=()` 且 `versions_seen` 内层全空，
      这正是 langgraph 会把 as_node 推断成 `__start__` 的前提条件。）
    """
    from langgraph.checkpoint.memory import InMemorySaver

    graph = _build_graph().compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": thread_id}}
    values: dict = {
        "session_id": thread_id,
        "mode": "agent",
        "status": "awaiting_submit" if with_problem else "dialog",
    }
    if with_problem:
        values["problem"] = _problem_meta()
    graph.update_state(cfg, values, as_node="generator_node")
    return graph, cfg


def test_write_as_node_choices_are_wired_as_documented():
    """锁定 write_as_node 的两个取值：都必须真实存在，且「等待提交」那个必须
    真的有一条静态边指向 wait_for_submit_node —— 这正是它能自愈的原因。"""
    b = _build_graph()
    edges = [tuple(e) for e in b.edges]
    assert write_as_node(True) == AS_NODE_AWAITING_SUBMIT
    assert write_as_node(False) == AS_NODE_NEUTRAL

    compiled = compile_graph()
    for node in (AS_NODE_AWAITING_SUBMIT, AS_NODE_NEUTRAL):
        assert node in compiled.nodes, f"as_node 候选 {node} 不在图里（节点被改名了？）"
    assert (AS_NODE_AWAITING_SUBMIT, "wait_for_submit_node") in edges, (
        f"{AS_NODE_AWAITING_SUBMIT} 必须静态边指向 wait_for_submit_node，"
        "否则写完状态推不出 next=('wait_for_submit_node',)"
    )


def test_generation_mirror_leaves_session_armed_for_run():
    """镜像注入后，有题的会话必须直接处于「等待提交」（next=wait_for_submit_node）。

    旧实现用 as_node="generator_node" 会留下 next=()，把整条链路押在 API 层兜底上。
    """
    graph, cfg = _degraded_session("mirror-armed", with_problem=False)
    graph.update_state(
        cfg,
        {"status": "awaiting_submit", "problem": _problem_meta()},
        as_node="generator_node",
    )
    mirror_state(
        graph,
        cfg,
        {"status": "awaiting_submit", "problem": _problem_meta(), "tutor_messages": []},
    )
    assert graph.get_state(cfg).next == ("wait_for_submit_node",)


def test_generation_mirror_does_not_fake_waiting_state_without_problem():
    """对话态（还没题）镜像注入后 next 必须保持 ()，不能伪造「等待提交」态。"""
    graph, cfg = _degraded_session("mirror-dialog", with_problem=False)
    mirror_state(graph, cfg, {"status": "dialog", "mode": "agent"})
    assert graph.get_state(cfg).next == ()


def test_pause_safe_update_after_mirror_keeps_run_alive():
    """事故核心回归：镜像注入后发一条对话，会话必须仍可 /run。

    旧代码此处省略 as_node → langgraph 推断成 `__start__` → 真的跑了一遍
    start_router → next 被钉成 ('agent_dialog_node',) → /run 永久 400。
    """
    graph, cfg = _degraded_session("mirror-then-chat", with_problem=True)
    assert graph.get_state(cfg).next == ()   # 出题镜像注入后的退化态

    pause_safe_update(graph, cfg, {"tutor_messages": []})   # 用户发一条对话消息

    nxt = graph.get_state(cfg).next
    assert "wait_for_submit_node" in nxt, (
        f"写对话历史后 next={nxt}：必须仍挂在 wait_for_submit_node 上，"
        "否则 /run 报 400「会话未在等待提交」，且卡死兜底（要求 next 为空）也救不回"
    )
    assert "agent_dialog_node" not in nxt, (
        "next 被 start_router 写成了 agent_dialog_node —— 说明写状态时又让 "
        "langgraph 自行推断了 as_node（推断成 __start__ 并执行了条件边）"
    )


def test_pause_safe_update_without_problem_keeps_next_empty():
    """对话阶段（无题）写状态不应把会话推进「等待提交」，否则 /run 语义错乱。"""
    graph, cfg = _degraded_session("dialog-chat", with_problem=False)
    pause_safe_update(graph, cfg, {"tutor_messages": []})
    assert graph.get_state(cfg).next == ()
