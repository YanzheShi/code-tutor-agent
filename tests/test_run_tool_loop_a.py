"""Plan A 验证：run_tool_loop 的 return_last_content 路径。

修复前 TEST5 的桩问题：把 fake_tool 注入到 tools 模块的命名空间，
使 getattr(_self_module, name) 能解析到（与真实 judge_run_code 一致）。
"""

import asyncio
import sys
import types


def _make_fake_llm(responses):
    """responses: list[ (content, tool_calls_list) ]，按顺序回放。"""
    class _AIMessage:
        def __init__(self, content, tool_calls):
            self.content = content
            self.tool_calls = tool_calls

    class _Bound:
        def __init__(self, seq):
            self._seq = list(seq)
        def invoke(self, messages):
            if not self._seq:
                return _AIMessage("", [])
            c, tcs = self._seq.pop(0)
            return _AIMessage(c, tcs)

    class _LLM:
        def __init__(self, seq):
            self._seq = seq
        def bind_tools(self, tools):
            return _Bound(self._seq)
    return _LLM(responses)


async def _run():
    from code_tutor_agent.agents import tools as tmod
    from code_tutor_agent.agents.tools import run_tool_loop

    results = []

    # 生产路径中 TUTOR_CHAT_TOOLS 永远非空，故测试统一用非空 fake 工具列表，
    # 让工具循环真正执行并捕获 last_content（tools=[] 会走 short-circuit 返回 ""）。
    _dummy_tool = types.SimpleNamespace(name="dummy_tool")

    # ── TEST1: 纯讨论（无 tool_calls）→ 返回 (messages, content) 元组
    llm = _make_fake_llm([("这是纯算法讨论的回答。", [])])
    res = await tmod.run_tool_loop(llm, [], tools=[_dummy_tool], return_last_content=True)
    ok = isinstance(res, tuple) and res[1] == "这是纯算法讨论的回答。"
    results.append(("TEST1 纯讨论返回元组", ok))

    # ── TEST2: 默认（return_last_content=False）→ 返回 list，向后兼容
    llm2 = _make_fake_llm([("讨论", [])])
    res2 = await tmod.run_tool_loop(llm2, [], tools=[_dummy_tool], return_last_content=False)
    ok2 = isinstance(res2, list)
    results.append(("TEST2 默认返回 list", ok2))

    # ── TEST3: tools=[] 且 return_last_content=True → 走 short-circuit，content 为 ""（已知行为）
    llm3 = _make_fake_llm([("x", [])])
    res3 = await tmod.run_tool_loop(llm3, [], tools=[], return_last_content=True)
    ok3 = isinstance(res3, tuple) and res3[1] == ""
    results.append(("TEST3 空 tools 返回空 content(short-circuit)", ok3))

    # ── TEST4: content 为 list[part] → 归一为纯文本
    class _PartMsg:
        def __init__(self):
            self.content = [{"text": "A"}, "B", {"text": "C"}]
            self.tool_calls = []
    class _BoundP:
        def invoke(self, messages):
            return _PartMsg()
    class _LLMP:
        def bind_tools(self, tools):
            return _BoundP()
    res4 = await tmod.run_tool_loop(_LLMP(), [], tools=[_dummy_tool], return_last_content=True)
    ok4 = isinstance(res4, tuple) and res4[1] == "ABC"
    results.append(("TEST4 list[part] 归一", ok4))

    # ── TEST5: 调了工具 → ToolMessage 被追加，last_content 为最终回复
    # 把 fake_tool 注入模块命名空间，使 getattr(_self_module, name) 可解析
    def fake_tool(q: str = "") -> str:
        return '{"ok": true, "echo": "%s"}' % q
    tmod.fake_tool = fake_tool  # 关键：让 _self_module.fake_tool 可解析

    class _AIMsgTC:
        def __init__(self, content, tool_calls):
            self.content = content
            self.tool_calls = tool_calls
    class _BoundTC:
        def __init__(self):
            self._first = True
        def invoke(self, messages):
            if self._first:
                self._first = False
                return _AIMsgTC("", [{"name": "fake_tool", "id": "call_1", "args": {"q": "hi"}}])
            return _AIMsgTC("最终基于工具结果的回答。", [])
    class _LLMTC:
        def bind_tools(self, tools):
            return _BoundTC()
    tool = types.SimpleNamespace(name="fake_tool")
    res5 = await tmod.run_tool_loop(_LLMTC(), [], tools=[tool], return_last_content=True)
    ok5 = isinstance(res5, tuple)
    msgs5, content5 = res5
    has_tool = any(getattr(m, "type", None) == "tool" for m in msgs5)
    ok5b = has_tool and content5 == "最终基于工具结果的回答。"
    results.append(("TEST5 调工具→ToolMessage+最终content", ok5 and ok5b))

    # 清理注入
    del tmod.fake_tool

    return results


if __name__ == "__main__":
    out = asyncio.run(_run())
    all_ok = True
    for name, ok in out:
        print(("PASS" if ok else "FAIL") + "  " + name)
        if not ok:
            all_ok = False
    print("\nALL_OK" if all_ok else "\nHAS_FAILURE")
    sys.exit(0 if all_ok else 1)
