"""验证 langgraph 语义：节点同时拥有静态 add_edge 出边、又返回 Command(goto=...) 时，
两条路径是否都会生效（目标节点都会执行）。

用法：
    uv run python scripts/verify_command_edge_conflict.py

对应仓库内的三处同型结构：
    - judge_node →(静态边) tutor_router_node，judge_node →(Command) tutor_node
    - agent_judge_node →(静态边) agent_tutor_node，agent_judge_node →(Command) update_profile_node
    - update_profile_node →(静态边) critic_node，update_profile_node →(Command) agent_tutor_node
"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command


class State(TypedDict):
    log: Annotated[list[str], operator.add]


def scenario_different_targets():
    """场景 1：Command(goto=B) + 静态边 A→C（目标不同）→ 预期 B、C 都执行。"""

    def node_a(state: State):
        return Command(update={"log": ["A"]}, goto="node_b")

    def node_b(state: State):
        return {"log": ["B"]}

    def node_c(state: State):
        return {"log": ["C"]}

    g = StateGraph(State)
    g.add_node("node_a", node_a)
    g.add_node("node_b", node_b)
    g.add_node("node_c", node_c)
    g.add_edge(START, "node_a")
    g.add_edge("node_a", "node_c")  # 静态边，与 Command(goto="node_b") 并存
    g.add_edge("node_b", END)
    g.add_edge("node_c", END)

    result = g.compile().invoke({"log": []})
    print(f"场景1（目标不同）执行序列: {result['log']}")
    both = "B" in result["log"] and "C" in result["log"]
    print(f"  → B 和 C 都执行了吗? {both}")
    return both


def scenario_same_target():
    """场景 2：Command(goto=B) + 静态边 A→B（目标相同）→ 预期 B 只执行一次。"""

    def node_a(state: State):
        return Command(update={"log": ["A"]}, goto="node_b")

    def node_b(state: State):
        return {"log": ["B"]}

    g = StateGraph(State)
    g.add_node("node_a", node_a)
    g.add_node("node_b", node_b)
    g.add_edge(START, "node_a")
    g.add_edge("node_a", "node_b")  # 与 Command 指向同一节点
    g.add_edge("node_b", END)

    result = g.compile().invoke({"log": []})
    print(f"场景2（目标相同）执行序列: {result['log']}")
    once = result["log"].count("B") == 1
    print(f"  → B 恰好执行一次吗? {once}")
    return once


def _langgraph_version() -> str:
    try:
        from importlib.metadata import version
        return version("langgraph")
    except Exception:
        return "unknown"


if __name__ == "__main__":
    print(f"langgraph 版本: {_langgraph_version()}\n")
    r1 = scenario_different_targets()
    print()
    r2 = scenario_same_target()
    print()
    if r1 and r2:
        print("结论: Command(goto) 不会覆盖静态边——目标不同时两个节点都会执行。")
        print("这解释了 graph.py 中 judge_node / agent_judge_node / update_profile_node")
        print("三处『静态边 + Command』结构会导致双节点执行（agent WA 路径因目标相同而幸免）。")
    else:
        print("结论: 与预期不符，请以实际输出为准复查 graph/state.py 的 attach_edge/_control_branch。")
