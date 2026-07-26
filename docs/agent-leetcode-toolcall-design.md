# Agent 工具调用能力 + LeetCode 对话出题 · 设计方案

> 状态：Phase 1-2 已实现（Phase 3 为可选/未来，见 §8）；工具调用能力已合入 `master`
> 分支：`master`（`feat/agent-toolcall` 为原开发分支，特性已并入主干；`agents/tools.py` 等改动均已落地）
> 目标：让 AI 导师在 agent 对话模式中，能够**自己决定**调用工具（尤其是“从 LeetCode 解析题目”与“现场判题验证”），把当前“只做结构化文本输出”的伪 agent 升级为“LLM 驱动工具调用”的真 agent。

---

## 0. 目标（一句话）

在 agent 对话模式里，用户贴一个 LeetCode 链接，AI 导师应当**自主调用解析工具**把题拉进来、确认后直接出题——而不是靠 SSE 层正则硬匹配，也不是靠前端单独点按钮。

更广义的目标：给 agent 真正接上 **tool calling** 能力，让 LLM 能自主决定调用工具，顺手补上当前“复杂任务处理能力缺失”的短板。

---

## 1. 现状盘点（哪些有、哪些缺）

### 1.1 已经具备的（直接复用，不重造）

| 能力 | 位置 | 说明 |
|---|---|---|
| LeetCode 解析逻辑 | `leetcode/leetcode_fetcher.py` | `fetch_problem(slug, domain)` 抓题；`problem_to_api_dict(p)` 转 API dict；`_parse_examples_to_test_cases` / `extract_function_signature` 解析用例与签名 |
| LeetCode REST 接口 | `api/routers/leetcode.py:15` `POST /leetcode/parse` | 当前由**前端**调用，不是 LLM 调用 |
| LeetCode 数据落 session | `schemas/state.py` `SessionState.leetcode` 字段 | generator 路径 A 消费它 |
| “消费 LeetCode”建题 | `nodes/generator.py:159` `_generate_from_leetcode()` | 走路径 A：跳过 LLM 出题，直接用解析数据建题 + 仅补一个 optimal_solution |
| 判题底层函数 | `sandbox/judge0_client.py` | `run_code` / `submit_test_cases` / `check_health` 都是本地 Python 函数 |
| 两个 MCP server 的**定义** | `mcp_server.py`、`mcp/judge0_server.py` | 定义了 judge_* 工具，但**运行中的应用从未 spawn 它们**（仅 `tests/` 里跑过） |

### 1.2 缺失的（本次要补的）

- ❌ **agent 没有任何工具调用通道**。`agent_dialog.py:400` 用 `with_structured_output(DialogIntent)`，`agent_judge.py:198` 用 `with_structured_output(JudgeAnalysis)`——都是纯结构化输出，**无 tool loop**。
- ❌ **LeetCode 解析对 LLM 不可见**：只有 REST 接口，agent 对话里 LLM 既不能调、也不会被触发去解析。
- ❌ **判题是节点硬写、不是 agent 调工具**：`agent_judge` 只是“读结果写反馈”，不能“动手跑代码验证”。
- ❌ **MCP 工具从未接进运行中的 agent**：`MultiServerMCPClient` / `get_tools` / `create_react_agent` 只出现在 `tests/test_agent.py`、`tests/test_mcp_server.py`，不在业务路径。

> 结论：现在这套“agent mode”本质是**带对话收集需求的图流程**，不是能自主调工具的 agent。

---

## 2. 架构决策

### 2.1 用 tool calling，不用 MCP

**MCP 与 tool calling 不是二选一的关系，是两个层：**

- **tool calling**（函数调用）：LLM 本身“决定调哪个函数、传什么参”的机制。这是 agent 的本质。
- **MCP**：只是“工具如何跨进程暴露”的传输协议，是工具*来源*的一种方式。不管用不用 MCP，LLM 底层跑的都是 tool calling。

**本项目选“本地 `bind_tools` 直接绑 Python 函数”，不上 MCP 子进程**，理由：

| 维度 | 本地 `bind_tools` | MCP server（独立进程） |
|---|---|---|
| 进程边界 | 无 | 多一个子进程 + stdio IPC |
| 调试/报错 | 栈清晰 | 序列化 + 跨进程，坑多 |
| 延迟 | 低 | 多一轮 IPC |
| 适用 | 工具就是自己的 Python 函数 | 工具是远端服务 / 需隔离 / 跨语言 / 多应用复用 |

本项目的 `fetch_problem`、`submit_test_cases`、`run_code`、`check_health` **全是自己包里的本地函数**，不跨团队、不需隔离、不跨语言 → MCP 是过度设计，徒增子进程与传输复杂度（而且现状是写了从没 spawn）。

> MCP server 可保留为“将来要把 judge 能力开放成独立服务”的可选外部接口，但**当下 agent 不应绕一圈去 spawn 它**。

### 2.2 Hybrid 边界（LLM 管什么 vs 代码管什么）

- **LLM 该管（tool calling）**：带歧义、需结合对话上下文*临时判断*的外部动作。
  - 用户贴了 LeetCode 链接 → 调 `parse_leetcode`
  - 辅导时想验证学生改法 → 调 `judge_run_code` / `judge_code`
- **代码该管（graph 节点，确定性）**：必须可靠、可重放的流程。
  - 题目生成（LLM 出 or LeetCode 导入）
  - 批量判题出 verdict（`generator_node` / `judge_node`）
  - DB 落库、状态机路由、`SessionPhase` 流转

> 即：**让 LLM 拥有工具调用能力来决定“何时做什么”，把确定性流程继续留在图节点里。** 这不是“把一切丢给 LLM”，而是“agent 范式里正确的分工”。

### 2.3 首批接入的两个能力域：解析（LeetCode）+ 判题（Judge0）

本次给 agent 接 tool calling，**不是只接 LeetCode 解析一个，而是把判题（Judge0）一并接进来**。二者的能力底层都现成，却都卡在“没进 LLM 工具通道”这同一根病根上（见 §1.2），应一起切入。它们恰好构成 agent 在对话里“**能动手**”的最小闭环：

| 能力域 | 工具（包现有本地函数） | 底层来源 | 解决的“掉链子” |
|---|---|---|---|
| 解析 LeetCode | `parse_leetcode(url)` | `leetcode/leetcode_fetcher.py` → `fetch_problem` / `problem_to_api_dict` | 解析只在前端 REST 可用，LLM 对话里既不能调、也不会被触发 |
| 判题 Judge0 | `judge_run_code` / `judge_code` / `judge_check_health` | `sandbox/judge0_client.py` → `run_code` / `submit_test_cases` / `check_health` | 判题只由图节点硬写，LLM 只能“读结果写反馈”，不能“动手跑代码验证” |

两者包的都是**自己包里的本地 Python 函数**，故用本地 `bind_tools` 直接绑（§2.1），全部 `asyncio.to_thread` 包同步调用防阻塞（§3）。

#### ⚠️ 重要边界：judge 工具 ≠ judge 节点（最易混淆，单独拎清）

- **`judge_run_code` / `judge_code`（工具）** = **交互式辅导能力**。对话里学生说“我这样改行不行”，agent 直接把代码**真跑一遍**验证、跑挂了现场定位、甚至 demo 正确写法。它是 LLM 在 tool loop 里**即兴**调用的，带歧义、需结合上下文判断，属于 §2.2“LLM 该管”的部分。
- **判题节点（graph `judge_node` / `generator_node` 批量出 verdict）** = **确定性判分流水线**。提交后批量跑用例、出最终 verdict、落库、路由——必须可靠、可重放，**留在图节点里，不交给 LLM 即兴发挥**。

> 一句话：**工具是导师“动手验证 / 演示”的手；节点是考试“正式判分”的机器。两者互补，工具不取代节点。** 本方案只把前者接入 tool calling，后者维持现状不动。

---

## 3. 工具定义（本地 `StructuredTool`，包现有函数）

新增文件 `agents/tools.py`（或并入 `agent_dialog.py`）：

```python
# agents/tools.py
from __future__ import annotations
import re, json, asyncio
from langchain_core.tools import StructuredTool
from code_tutor_agent.leetcode.leetcode_fetcher import fetch_problem, problem_to_api_dict
from code_tutor_agent.sandbox.judge0_client import run_code, submit_test_cases, check_health


def _parse_leetcode(url: str) -> str:
    m = re.search(r"/problems/([^/]+)", url.strip().rstrip("/"))
    if not m:
        return json.dumps({"error": "无效的 LeetCode 链接，请粘贴完整题目 URL"})
    slug = m.group(1)
    domain = "leetcode.cn" if ".cn" in url else "leetcode.com"
    try:
        p = fetch_problem(slug, domain=domain)
        return json.dumps(problem_to_api_dict(p), ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"获取题目失败: {e}"})


async def parse_leetcode(url: str) -> str:
    # fetch_problem 是同步 urllib 调用，丢到线程避免阻塞事件循环
    return await asyncio.to_thread(_parse_leetcode, url)


def _judge_run_code(source_code: str, stdin: str = "") -> str:
    r = run_code(source_code, stdin=stdin)
    return json.dumps({
        "stdout": r.stdout, "stderr": r.stderr,
        "status": r.status_desc, "verdict": r.verdict(),
        "time_ms": r.runtime_ms(), "memory_kb": r.memory_kilobytes,
    }, ensure_ascii=False)


async def judge_run_code(source_code: str, stdin: str = "") -> str:
    return await asyncio.to_thread(_judge_run_code, source_code, stdin)


def _judge_code(source_code: str, test_cases_json: str) -> str:
    tcs = json.loads(test_cases_json) if isinstance(test_cases_json, str) else test_cases_json
    results = submit_test_cases(source_code, tcs)
    passed = sum(1 for r in results if r.get("status") == "Passed")
    return json.dumps({
        "results": results,
        "summary": {"total": len(results), "passed": passed, "all_passed": passed == len(results)},
    }, ensure_ascii=False)


async def judge_code(source_code: str, test_cases_json: str) -> str:
    return await asyncio.to_thread(_judge_code, source_code, test_cases_json)


async def judge_check_health() -> str:
    return json.dumps(await asyncio.to_thread(check_health), ensure_ascii=False)


# 导出给 agent 绑定
AGENT_TOOLS = [
    StructuredTool.from_function(
        func=parse_leetcode, name="parse_leetcode",
        description="当用户提供 LeetCode 题目链接(leetcode.com 或 leetcode.cn 的 /problems/xxx)时调用，"
                    "解析并返回标题/描述/示例/约束/模板代码等结构化数据，用于直接作为练习题。",
    ),
    StructuredTool.from_function(
        func=judge_run_code, name="judge_run_code",
        description="执行任意 Python 代码片段并返回 stdout/stderr/状态，用于辅导时验证思路或演示。",
    ),
    StructuredTool.from_function(
        func=judge_code, name="judge_code",
        description="用一批测试用例判题(LeetCode 风格 class Solution)，返回每个用例通过与汇总。",
    ),
    StructuredTool.from_function(
        func=judge_check_health, name="judge_check_health",
        description="检查判题后端(Judge0)是否存活。",
    ),
]
```

> 注：`fetch_problem` 是同步 `urllib`，在 async agent 循环里用 `asyncio.to_thread` 包一层，避免阻塞事件循环。

---

## 4. Dialog Agent 改造

### 4.1 当前（伪 agent）

`agent_dialog.py` 的 `analyze_user_intent()`：
```python
structured_llm = llm.with_structured_output(DialogIntent)
result = structured_llm.invoke([("system", ...), ("human", ...)])  # 只吐 JSON，无工具
```

### 4.2 目标（带工具的 agent 循环）

```python
# agent_dialog.py — analyze_user_intent 改造
from code_tutor_agent.agents.tools import AGENT_TOOLS

llm = get_llm(model_alias, temperature=0.7)
llm_with_tools = llm.bind_tools(AGENT_TOOLS)

messages = [("system", system_prompt), ("human", user_prompt)]
tool_results: dict = {}
MAX_TOOL_ROUNDS = 3

for _ in range(MAX_TOOL_ROUNDS):
    ai = llm_with_tools.invoke(messages)
    if not getattr(ai, "tool_calls", None):
        break
    for tc in ai.tool_calls:
        fn = next((t for t in AGENT_TOOLS if t.name == tc["name"]), None)
        if fn is None:
            continue
        # 执行工具：本 langchain 版本 StructuredTool.ainvoke 对 async func 不会正确
        # await（返回未执行的协程），故直接 await 工具背后的原始异步函数。
        fn = TOOL_FUNCS[tc["name"]]              # 原始 async 函数（见 agents/tools.py）
        out = await fn(**tc["args"])            # 异步执行工具
        if tc["name"] == "parse_leetcode":
            tool_results["leetcode"] = out           # 暂存解析结果
        messages.append(ai)
        messages.append({
            "role": "tool", "content": out, "tool_call_id": tc["id"],
        })

# 工具用完后再做结构化意图判定（仍复用 DialogIntent）
intent = (llm.with_structured_output(DialogIntent)).invoke(messages)
intent._leetcode_payload = tool_results.get("leetcode")   # 非标准字段，仅编排层消费
return intent
```

### 4.3 `DialogIntent` 扩展

`agent_dialog.py` 的 `DialogIntent` 增加两个可选字段，供路由区分来源：

```python
class DialogIntent(BaseModel):
    topic: str = ""
    difficulty: str = ""
    is_ready: bool = False
    next_message: str = ""
    # —— 新增 ——
    source: Literal["generated", "leetcode"] = "generated"
    leetcode_url: str = ""   # source==leetcode 时记录原始链接
```

> 注意：`_leetcode_payload`（工具实际返回的解析 dict）不走 Pydantic，由编排层从 `tool_results` 读取，避免污染结构化意图模型。

### 4.4 路由衔接（agent_dialog_node）

在图节点 `agent_dialog_node` 里，若 `intent.source == "leetcode"` 且解析成功：

```python
if intent.source == "leetcode" and leetcode_payload:
    update_state(config, {"leetcode": json.loads(leetcode_payload)}, as_node="agent_dialog_node")
    # generator_node 走路径 A：_generate_from_leetcode，跳过 LLM 出题
```

`generator.py` 的 `_generate_from_leetcode` 已能消费 `state.leetcode`，**无需改动**。

---

## 5. 端到端流程（用户贴 LeetCode 链接 → 出题）

```
用户: "帮我做这道 https://leetcode.cn/problems/two-sum"
  │
  ▼ chat SSE → stream_dialog_response / analyze_user_intent
agent_dialog（bind_tools）:
  LLM 识别到 LeetCode 链接 → 调 parse_leetcode(url)
  tool 返回题目结构化数据 → 暂存 tool_results["leetcode"]
  LLM 产出 DialogIntent{ is_ready=True, source="leetcode", leetcode_url=... }
  │
  ▼ agent_dialog_node：写 state.leetcode = 解析数据
  │
  ▼ planner_node → generator_node（路径 A：_generate_from_leetcode）
  直接用解析数据建题 + 仅 LLM 补 optimal_solution → wait_for_submit_node
  │
  ▼ 用户写代码 → judge → tutor
```

> 前端现有的 `POST /leetcode/parse` 入口**保留不动**（仍可用于“不进对话、直接开练”的路径），两路并行不冲突。

---

## 6. 涉及文件与改动清单

| 文件 | 改动 |
|---|---|
| `agents/tools.py` | **新增**：定义 `parse_leetcode` / `judge_run_code` / `judge_code` / `judge_check_health` 四个本地 `StructuredTool` |
| `agents/agent_dialog.py` | `DialogIntent` 加 `source` / `leetcode_url`；`analyze_user_intent` 改为带工具循环；`build_initial_message` 可引导“也可贴 LeetCode 链接” |
| `agents/agent_dialog.py` 中的图节点 `agent_dialog_node` | 读取 `tool_results["leetcode"]` → 写 `state.leetcode` |
| `nodes/generator.py` | **不改**（路径 A 已就绪），仅确认 `state.leetcode` 字段消费一致 |
| `schemas/state.py` | **不改**（`leetcode` 字段已有） |
| `mcp_server.py` / `mcp/judge0_server.py` | **保留**，作为可选外部接口，不在本次运行路径使用 |
| `tests/` | 新增 `test_agent_tools.py`：单测四个工具 + 贴链接端到端意图判定 |

---

## 7. 风险与应对

| 风险 | 应对 |
|---|---|
| 工具循环里又踩 `dict`/`Message` 类型混用（历史 bug） | 工具入参/出参严格 str/JSON；`messages` 全程用 LangChain Message 对象，不混 dict |
| LLM 在没链接时也乱调 `parse_leetcode` | tool description 明确“仅在用户提供 LeetCode 链接时调用”；`MAX_TOOL_ROUNDS=3` 限循环 |
| `fetch_problem` 网络失败 / 429 限流 | `_parse_leetcode` 已 try/except 返回 `{"error": ...}`；agent 拿到 error 后转为自然语言安抚，不崩溃 |
| 解析数据缺 `starter_code`（部分题无 Python 片段） | `_generate_from_leetcode` 已有兜底；必要时让 LLM 据描述补模板 |
| 工具调用增加延迟/cost | 仅在 detect 到链接或辅导需验证时才触发工具；普通“随机出题”路径不进工具循环 |
| 事件循环被同步 `urllib` 阻塞 | `parse_leetcode` 等用 `asyncio.to_thread` 包同步实现 |

---

## 8. 落地步骤（分阶段，建议先小后大）

- **Phase 1（✅ 已实现）**
  1. 新增 `agents/tools.py`，定义四个本地 `StructuredTool`（先不强制全用）。
  2. `agent_dialog.py` 改造成带工具循环；`DialogIntent` 加 `source` / `leetcode_url` / `leetcode_payload`。
  3. `api/routers/chat.py` 的 agent-dialog 两处分支 `await analyze_user_intent` 并把 `leetcode_payload` 写入 `state.leetcode`（generator 路径 A 复用）。
  4. 单测 `tests/test_agent_tools.py`（全 mock，离线绿）。
- **Phase 2（✅ 已实现）**
  5. 把 `judge_run_code` / `judge_code` 接进**辅导**环节：
     - 复用通用工具循环 `run_tool_loop(llm, msgs, tools=JUDGE_TOOLS)`（`agents/tools.py`），导师可现场跑学生改法、验证提示、演示。
     - 接入点在 `api/routers/chat.py` 的**两处导师对话分支**（流式 `normal_chat_stream` / 非流式 `chat_with_tutor`）。
       注意：这两处是 API 层直接调 LLM（graph 停在 END / interrupt 时 `chat_node` 不可靠，
       详见 `chat.py:176` 注释），故**不进 graph 节点**，与 Phase 1 改 agent-dialog 分支思路一致。
     - `JUDGE_TOOLS` 只暴露 judge 三类工具，不包含 `parse_leetcode`（出题解析属于对话意图分析阶段，见 §2.3 边界）。
- **Phase 3（可选 / 未来）**
  6. 若 judge 要开放成独立服务，再评估是否起 MCP server；否则保持本地 `bind_tools`。

---

## 9. 明确不做的事

- ❌ 不把题目生成、批量判题出 verdict、DB 落库、状态机路由搬进 LLM 工具调用——这些确定性流程留在 graph 节点。
- ❌ 不为了“接工具”而去 spawn MCP 子进程；本地 `bind_tools` 更贴合现状。
- ❌ 不删除现有 `POST /leetcode/parse` REST 接口（前端路径保留）。
- ❌ 不做之前提过的“一次性预生成 15 题题目组”——本方案聚焦 agent 工具能力与 LeetCode 对话出题，题目组需求另行评审。

---

## 附：关键结论回顾

1. 现状 agent **没有任何工具调用通道**，MCP server 定义了但从没接进运行中的应用。
2. 正确的做法 = **tool calling（本地 `bind_tools`）+ Hybrid 分工**，不是 MCP 子进程。
3. LeetCode 对话出题 = 给 dialog agent 加 `parse_leetcode` 工具 + 写 `state.leetcode`，**generator 路径 A 直接复用**，改动小、收益大。
4. 首批接入的是**两个能力域**：解析（LeetCode）+ 判题（Judge0），二者同病根、一起切入；`judge_*` 工具只用于对话里**交互式辅导验证**，不取代图里确定性判题节点。
