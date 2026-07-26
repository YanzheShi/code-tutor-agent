# CodeTutor Agent — 系统架构与完整流程梳理

> 适用分支：`master`（`problem_agent` 与 LangSmith 已合入；出题能力已收敛为独立 **Problem Agent**，见 `agents/agent_problem.py` 与 `docs/problem-agent-flow.md`）
> 目标：从「节点 → 条件路由 → 流转逻辑 → 循环机制 → 分层架构」五个层次，完整、可复盘地描述本系统如何运转。
> 配套设计文档：
> - `agent-leetcode-toolcall-design.md`（仅覆盖 tool-calling 增强部分）
> - `problem-agent-flow.md`（**出题 Agent 内部流程图**：`LLM → adapter → cli → static` 降级链，本文件是全局视角）

---

## 0. 一图速览（分层架构）

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              前端 (frontend/  React)                       │
│   编辑器 / 对话面板 / 进度轮询 / 下一题按钮                                  │
└───────────────┬───────────────────────────────────────────┬──────────────┘
                │ HTTP (SSE)                                  │
┌───────────────▼───────────────────────────────────────────▼──────────────┐
│                         API 层  (api/routers/)                            │
│  session.py  chat.py  run.py  problems.py  leetcode.py  admin.py          │
│  · 是 graph 的「驱动器」：create / submit / next-problem 调 graph.invoke    │
│  · 也大量「旁路」graph：普通聊天 / agent 对话 直接在 API 层调 LLM           │
│  · generation.py 后台任务：跑 graph + 后台生成完整测试用例                  │
└───────────────┬───────────────────────────────────────────┬──────────────┘
                │ get_graph() 单例                            │ update_state / get_state
┌───────────────▼───────────────────────────────────────────▼──────────────┐
│                   LangGraph StateGraph (graph/graph.py)                   │
│  节点 (13 个) + 条件路由 (start/planner/wait_for_submit/tutor_router)      │
│  状态机由 checkpointer (SqliteSaver) 托管 —— 暂停 / 恢复 / 跨请求持久      │
└───────────────┬───────────────────────────────────────────┬──────────────┘
                │                                            │
┌───────────────▼──────────┐                   ┌─────────────▼──────────────┐
│  agents/ (LLM 编排)       │                   │  sandbox/ (判题执行)        │
│  · agent_dialog (意图分析) │                   │  · runner (run_solution)    │
│  · agent_judge (LLM 判题) │                   │  · adversarial (对抗测试)   │
│  · agent_problem (出题 Agent)│                 │  · judge0_client (Judge0)   │
│  · tools (tool-calling 循环)│                  │  · input_generator          │
└───────────────┬──────────┘                   └─────────────┬──────────────┘
                │                                            │
┌───────────────▼─────────────────────────────────────────────────────────┐
│  支撑层：config (LLM 别名) · db (SQLite 题目/提交/画像) · profile (画像)    │
│          · context_manager (对话压缩/跨题摘要) · store (InMemoryStore)      │
│          · leetcode (LC 解析) · mcp_server (Judge0 MCP 服务，可选)          │
└──────────────────────────────────────────────────────────────────────────┘
```

**一句话**：前端只跟 API 说话；API 一部分请求会「进入 graph 跑状态机」（出题/判题/辅导），另一部分请求「绕过 graph 直接在 API 层调 LLM」（自由聊天/导师对话），保证流式与可靠性。

---

## 1. 核心状态：`SessionState`

`SessionState`（`schemas/state.py`）是流经 StateGraph 的**单一数据源**，由 checkpointer 跨请求托管。关键字段按用途分组：

| 分组 | 字段 | 说明 |
|------|------|------|
| 身份 | `session_id`、`mode` (`practice/interview/debug_theatre/agent`) | 会话维度 |
| 元状态 | `status` (`awaiting_problem/awaiting_submit/judging/tutoring/dialog/done/error`)、`phase` (`clarifying/dialog/solving/reviewing/done`) | `status`=内部驱动态；`phase`=前端消费态 |
| 题目 | `problem` (ProblemMeta)、`leetcode` (导入数据)、`submissions` (累加) | 当前题 & 提交历史 |
| 辅导 | `hint_level` (0-4)、`turns_in_level`、`tutor_messages`、`last_verdict`、`adversarial_triggered` | WA 线辅导上下文 |
| Agent | `agent_dialog_history`、`agent_dialog_complete`、`warm_feedback`、`repair_suggestion`、`judge_cycle` | Agent 模式专用 |
| 画像 | `profile_delta` | 由 judge 产出、被 update_profile_node 消费 |
| 跨题 | `problem_history`、`total_problems`、`last_diagnosis`、`context_summary`、`pending_abandon`、`next_preference` | 换题/多题维度 |

> **三个生命周期**（state.py 头部注释）：
> - 普通模式：`START → planner → generator → wait_for_submit → judge → tutor →（循环回 wait）`
> - Agent 模式：`START → agent_dialog(多轮) → planner → generator → wait → agent_judge → agent_tutor →（AC 停 / WA 循环）`
> - 评审批注：评审不拦 AC，只影响下一题难度与画像。

---

## 2. LangGraph 节点清单（13 个）

| 节点 | 性质 | 职责 | 主要输出 / 路由 |
|------|------|------|----------------|
| `planner_node` | 规则引擎 | 按画像/偏好选 topic+difficulty，或复用 dialog 结果 | `problem?` → wait : generator |
| `generator_node` | LLM+本地 | **内联降级**（LLM→adapter→static，**无 cli 通道**）→ 本地跑 2 个样例 I/O → 落库；LeetCode 导入走 path A。**注意：未调用 `ProblemAgent.generate()`**（见 §10） | 固定 → `wait_for_submit_node` |
| `wait_for_submit_node` | **唯一 interrupt** | 暂停等用户代码；resume 时收代码入 `submissions` | → judge(普通) / agent_judge(agent) |
| `judge_node` | 三阶段判题 | 基础→对抗→评审，确定性流水线 | 永远 → `tutor_router_node` |
| `tutor_router_node` | LLM/规则 | 读末条用户消息+情绪，决定 CONTINUE/ESCALATE/RESOLVED | RESOLVED→wait；其余→`tutor_node` |
| `tutor_node` | 决策树+LLM | L0-L4 渐进提示；AC/对抗失败反馈 | → `constitutional_guard_node`(WA) 或 `update_profile_node`(AC/对抗) |
| `critic_node` | 评审+flush | flush 当前题→`problem_history`，宪法 R01/R04 检查，路由 | ABANDON→planner；AC→END；WA→wait |
| `update_profile_node` | 单写者 | 消费 `profile_delta` 写 store（**唯一**允许写画像的节点） | → `critic_node` |
| `agent_dialog_node` | 对话入口 | 出题前多轮；未完成→暂停；完成→planner | 完成→planner；否则→END |
| `agent_judge_node` | LLM 判题 | Judge0 跑用例 + LLM 解读生成温暖反馈 | → `agent_tutor_node` |
| `agent_tutor_node` | 纯路由 | 看 verdict 决定下一跳 | AC→END；否则→wait |
| `chat_node` | 流式聊天 | 处理 `state.messages` 里的用户消息（见 §6 备注） | → END |
| `constitutional_guard_node` | 后置守卫 | R09/R10 扫描 tutor 消息，违规则替换 | → END |

> 节点全部通过 `Command(update=..., goto=...)` 显式路由；只有 `wait_for_submit_node` 用 `interrupt()` 暂停。

---

## 3. 边与条件路由（完整逻辑）

图定义在 `graph/graph.py`，由 4 个条件路由函数驱动：

### 3.1 `start_router`（`__start__` →）
```
若 state.messages 末条是 Human/user 消息        → chat_node
否则 若 state.mode == "agent"                   → agent_dialog_node
否则                                          → planner_node
```

### 3.2 `planner_router`（`planner_node` →）
```
若 state.problem 已存在                        → wait_for_submit_node   (短路，不重复出题)
否则                                          → generator_node
```

### 3.3 `wait_for_submit_router`（`wait_for_submit_node` →）
```
若 state.mode == "agent"                       → agent_judge_node
否则                                          → judge_node
```

### 3.4 `tutor_router_node` 内部决策（`judge_node` 之后）
```
用户说"懂了/提交/AC/过了"等关键词              → RESOLVED → wait_for_submit_node
挫败 + 同 level≥2 轮  或  同 level 超上限      → ESCALATE → tutor_node (hint+1)
默认                                          → CONTINUE → tutor_node
```
（细节：先试 LLM router，失败回退规则 router。）

### 3.5 固定边
```
generator_node      → wait_for_submit_node
judge_node          → tutor_router_node
constitutional_guard_node → END
update_profile_node → critic_node
agent_judge_node    → agent_tutor_node
chat_node           → END
```

### 3.6 节点内部 `Command(goto=...)` 路由汇总
```
tutor_node:
   AC + 对抗失败  → update_profile_node
   AC（全过）     → update_profile_node
   base_fail(WA)  → constitutional_guard_node
critic_node:
   verdict=ABANDON → planner_node
   verdict=AC      → END (phase=reviewing)
   其他(WA)       → wait_for_submit_node
agent_tutor_node:
   verdict=AC      → END (phase=reviewing)
   其他(WA/RE/TLE)→ wait_for_submit_node
agent_dialog_node:
   dialog 完成     → planner_node
   未完成          → END (status=dialog，暂停等 SSE 对话)
```

---

## 4. 主流程生命周期（端到端）

### 4.1 普通模式（practice / interview）

```
[创建会话] POST /session
   └─ session.py.create_session
        ├─ LeetCode 快速路径? → run_fast_path（直接落库，跳过 graph 生成）
        └─ 否则 → asyncio.create_task(run_generation)
                     └─ generation.py.run_generation:
                          graph.invoke(initial)        ← 进入 graph
                             start_router → planner → generator → wait_for_submit(interrupt 暂停)
                          （graph 此刻 PAUSED 在 wait_for_submit_node）
                          _generate_complex_tests(pid)  ← 后台补 10+ 用例
   前端轮询 GET /session/{sid}/state 直到 problem 出现

[用户写代码 → 点提交] POST /session/{sid}/submit
   └─ graph.invoke(Command(resume={"code":...}))
        resume @ wait_for_submit_node → 收代码进 submissions(status=judging)
        → judge_node（三阶段：基础→对抗→评审）
            ├─ 基础 WA → tutor_router → tutor_node(L0-L4) → constitutional_guard → END(awaiting_submit)
            ├─ AC 但对抗挂 → tutor_node(对抗反馈) → update_profile → critic(WA) → wait(interrupt 暂停)
            └─ 全过 AC → tutor_node(正向+评审卡) → update_profile → critic(AC) → END(phase=reviewing)
   返回 verdict + tutor_message；前端展示

[WA 循环] 用户改代码 → 再点提交 → 再次 Command(resume) → 从 __start__ 重跑
   （重跑时 planner 见 problem 已存在 → 短路到 wait_for_submit(interrupt) → 消耗新代码）
   → judge → tutor → ... 直到 AC

[AC 后换题] POST /session/{sid}/next-problem (普通模式)
   └─ update_state(pending_abandon=true, as_node=critic_node) + graph.invoke(None)
      → critic(见 ABANDON) → planner(problem 已清空) → generator → wait(interrupt)
```

### 4.2 Agent 模式（dialog + MCP 式判题）

```
[创建会话] POST /session  (mode=agent)
   └─ run_generation → graph.invoke(initial)
        start_router(mode=agent) → agent_dialog_node
          首次：发欢迎语，pause @ END(status=dialog)

[多轮对话确定需求] POST /session/{sid}/chat/stream  (status=dialog & mode=agent & 未完成)
   └─ chat.py 直接（不进 graph）：
        history = agent_dialog_history + 当前消息
        analyze_user_intent(history)   ← LLM 结构化意图（可能调 parse_leetcode 工具）
           ├─ is_ready=false → 回复追问，继续对话
           └─ is_ready=true  → 写 agent_dialog_complete=true, topic/difficulty, (leetcode)
                               BackgroundTasks 触发 graph.invoke(dict(values))
                                  agent_dialog_node(完成) → planner → generator → wait(interrupt)
   前端轮询 state，problem 出现后进入做题

[提交] POST /session/{sid}/submit
   └─ Command(resume={"code"}) → resume @ wait_for_submit
        wait_for_submit_router(mode=agent) → agent_judge_node
           Judge0 跑用例 + LLM 解读 → 温暖反馈/修复建议
        → agent_tutor_node
           ├─ AC  → END(phase=reviewing)
           └─ WA  → wait_for_submit_node(interrupt 再次暂停)   ← Agent 模式循环点
   用户改代码 → 再提交 → resume @ wait(interrupt) → agent_judge → agent_tutor → ... 直到 AC

[换题] POST /session/{sid}/next-problem (agent 模式)
   └─ 生成跨题摘要 context_summary，重置 agent_dialog_history=[引导语]
      update_state(agent_dialog_complete=false, status=dialog) + graph.invoke(None)
      → agent_dialog_node(未完成) → END(status=dialog)
   回到 4.2 开头多轮对话
```

---

## 5. 循环与暂停机制（本系统最关键的「运转方式」）

> 一句话：**graph 从不在内存里长跑成一个大循环。它靠 checkpointer 反复「重进 → 短路 → 暂停」来制造循环。**

### 5.1 两个暂停点
- **`wait_for_submit_node` 的 `interrupt()`**：唯一真正的暂停。graph 在此返回控制权给 API 层，等待用户代码。
- **`__end__` 自然到达**：WA/AC 流程走到 END 即「本次 graph 运行结束」（仍被 checkpointer 持久化）。

### 5.2 普通模式的循环（每次提交 = 一次完整重跑）
1. 生成阶段：`graph.invoke(initial)` 跑 `planner→generator→wait(interrupt)`，**PAUSED**。
2. 第 1 次提交：`graph.invoke(Command(resume={code}))` → 从 interrupt 恢复，跑 `judge→tutor→...→END`（**完成**）。
3. 第 2 次提交：graph 已到 END，`Command(resume={code2})` 被 LangGraph 当作「新输入重新从 `__start__` 跑」；`start_router→planner`（**problem 已存在，短路**）`→wait(interrupt)`，interrupt 把 `code2` 当作 resume 值消费。
4. 之后每轮提交重复第 3 步。

### 5.3 Agent 模式的循环（停在 interrupt 不触达 END）
- 提交后 `agent_tutor_node(WA) → wait_for_submit_node` 再次 `interrupt()`，**一直 PAUSED**，不触达 END。
- 每次提交 `Command(resume={code})` 直接在该 interrupt 恢复，连续重跑到下一次 AC。
- 只有 `verdict=AC` 时才走到 `END(phase=reviewing)`，等待用户点「下一题」。

### 5.4 为什么这么设计
- `interrupt()` 让「出题生成」与「用户写代码」这两个时间跨度极大的步骤天然解耦。
- `planner_node` 的 `problem` 短路保证「重跑不会重复出题」。
- 换题用 `update_state(..., as_node="critic_node")` 把「当前节点」伪造成 critic，使 `graph.invoke(None)` 从 critic 继续，而非从头——这是实现「flush 当前题→生成新题」的关键技巧。

---

## 6. API 层如何驱动 graph（入口 vs 旁路）

| 端点 | 进 graph? | 说明 |
|------|-----------|------|
| `POST /session` | ✅ | `run_generation` 后台 `graph.invoke` 出题 |
| `POST /session/{sid}/submit` | ✅ | `Command(resume=code)` 恢复判题/辅导 |
| `POST /session/{sid}/next-problem` | ✅ | `update_state(as_node=...)` + `graph.invoke` 换题 |
| `POST /session/{sid}/run` | ❌ | 直接 `run_solution` 跑可见用例，结果写 `last_run_results` |
| `GET /session/{sid}/state` | ❌ | `graph.get_state` 只读序列化 |
| `GET /session/{sid}/reference` | ❌ | 仅 AC 后读 DB 参考解 |
| `POST /session/{sid}/chat/stream` | ⚠️ 条件 | **agent-dialog 分支**：API 直接调 `analyze_user_intent`，用 `BackgroundTasks` 触发 `graph.invoke` 出题；**普通辅导分支**：完全旁路 graph，直接 LLM（+工具循环）。 |
| `POST /session/{sid}/chat` | ⚠️ 条件 | 同上的非流式版本 |
| `POST /leetcode/parse` | ❌ | 直接调 `leetcode_fetcher` 解析 |
| `/admin/*`、`/problems` | ❌ | 直接读 DB |

> **为什么普通聊天要旁路 graph？** `chat.py:176-180` 注释明确：graph 停在 `interrupt` 或 `END` 时，`graph.stream` 不会从 END 重启（返回空），且 `SqliteSaver` 不支持 `astream`。统一在 API 层直接调 LLM 最可靠。因此 `chat_node` 在图里存在，但**当前 API 不经由它**——它属于预留/历史节点（由 `state.messages` 驱动，而 API 聊天改的是 `tutor_messages`）。

---

## 7. Agent 工具调用循环（tool-calling 增强）

位于 `agents/tools.py` 与 `agents/agent_dialog.py`：

### 7.1 工具集
- `parse_leetcode(url)`：解析 LC 题目（对话意图分析阶段用）。
- `judge_run_code / judge_code / judge_check_health`：包装 Judge0，跑代码/判题/探活。

### 7.2 `run_tool_loop(llm, messages, tools, max_rounds=3)`（通用循环）
- LLM 自主决定调工具；`bind_tools` 后 `invoke` 看 `tool_calls`。
- 工具函数通过 `getattr(_self_module, name)` **动态解析**（便于测试 mock），并**直接 `await` 原始 async 函数**（绕开 `StructuredTool.ainvoke` 的协程坑）。
- 工具异常统一转成 `{"error": ...}` JSON，不崩；`max_rounds` 防空转；只执行白名单工具，未绑定工具忽略即停。

### 7.3 两处使用
- **意图分析**（`agent_dialog.py`）：仅当用户贴了 LC 链接才绑 `parse_leetcode`，解析结果挂到 `DialogIntent.source/leetcode_payload`，由 `chat.py` 写入 `state.leetcode` → generator path A 直接建题。
- **辅导环节**（`chat.py` 流式/非流式分支）：绑 `JUDGE_TOOLS`（`judge_run_code/judge_code/judge_check_health`），导师可现场跑代码验证。**不取代** graph 里的 `judge_node` 确定性批量判题。
- 边界：`parse_leetcode` 属「对话意图分析」；`judge_*` 属「交互式动手验证」，二者均不取代节点流水线（详见 `agent-leetcode-toolcall-design.md §2.3`）。

---

## 8. 判题流水线 & 辅导决策树 & 宪法守卫

### 8.1 `judge_node` 三阶段（普通模式）
1. **基础判题**：跑全量用例。挂 → 直接交辅导（不浪费 token 跑对抗）。
2. **对抗测试**（仅基础 AC 后）：边界枚举 + 规模对抗，任何一步挂 → 交辅导带原因。
3. **多维评审**（全过）：LLM 复杂度/风格点评，**不拦 AC**，只影响下一题与画像。
- 数据粒度：每个阶段一个 `JudgeResult`，细粒度控制路由（PRD §3.2）。

### 8.2 `tutor_node` L0-L4 决策树（WA 线）
- 纯规则定深浅：`同类错误≥3→插讲解`；`多次提交+情绪→L4`；`有 diff 且方向对→跳级`；`方向错→L1/L2`；默认保持。
- LLM 定内容：按 `hint_level` 生成提示。
- **双重防泄漏**：`tutor_node._post_guard_scan`（R01/R10）+ 独立 `constitutional_guard_node`（R09/R10），低等级（<4）禁止泄露完整代码、禁止代写。

### 8.3 `critic_node` 宪法评审
- flush 当前题 → `problem_history`；R01 检查 tutor 消息、R04 检测用户挫败情绪；按 verdict 路由（ABANDON→planner / AC→END / WA→wait）。
- **单写者纪律**：只有 `update_profile_node` 可写画像 store；judge/tutor 只能挂 `profile_delta`。

---

## 9. 用户画像模块

- 结构（v2）：`prof / stab / forget / errors / attempts`（per-tag）。
- `update_profile_node`：消费 `state.profile_delta` → `apply_delta` → 写 `InMemoryStore` + 落 SQLite。
- 选题时 `planner._select_topic_by_v2_profile` 综合「弱(prof低)+忘(decay低)+不稳(方差大)」打分，选最该练的 tag 出题。
- 对话开场 `build_initial_message` / `analyze_user_intent` 会把弱项摘要注入 prompt 引导推荐方向。

---

## 10. 后台生成与测试用例生成

`api/services/generation.py`：
- `run_generation`：`graph.invoke` 带超时（默认 120s），超时/失败自动降级**静态题库**（`store/static_pool.py`）。
- **出题入口的两套实现（务必分清，见交接文档 §5.1）**：
  - **(A) `ProblemAgent`（`agents/agent_problem.py`，详见 `problem-agent-flow.md`）**：可复用的四通道降级能力 `LLM → adapter → cli → static`，返回 `GenerationOutcome(problem, channel, error)`；`problem_generator.py` 已退化为只 re-export `agent_problem` 的向后兼容 shim。`verify_problem` 自校验（去围栏 / 防思维链泄漏 / `compile` 最优解 / 推导 `starter_code`）与 `max_tokens=4096` 限流（修复 Bug7 截断）都在这一层。
  - **(B) `generator_node`（`nodes/generator.py`，当前 graph 实际出题路径）**：**自己内联**了一套 `LLM → adapter（engine_adapter.generate_problem）→ static` 降级，**没有 cli 通道、也不调用 (A) 的 `ProblemAgent.generate()`**。`generator_node` 与 `run_generation` 走的是 (B)，不是 (A)。
  - **⚠️ 已知技术债**：两套降级逻辑并存且 (B) 未复用 (A) 的 cli 逃生舱；若未来要统一，建议让 `generator_node` 改调 `ProblemAgent.generate()`（或反之），避免出题契约在两条路径上漂移。
- `_generate_complex_tests`：用参考解跑 12 组随机输入 + LLM 生成边界用例 → 合并更新 DB 测试套件（用户写代码期间异步完成）。**注意：运行时后台造用例走这里 + 对抗用例生成，并不走 `skills/defs/cta-generate-test-cases` 这个独立 skill（该 skill 当前未接入运行时，见交接文档 §6.3）。**
- LeetCode 快速路径 `run_fast_path`：跳过 graph 出题，直接落库 + 后台补最优解/用例。

---

## 11. 关键设计决策与已修复坑（速查）

1. **graph 反复重进而非长跑循环**（§5）：靠 checkpoint 短路 + interrupt 制造循环，是系统能「跨请求」运转的核心。
2. **普通聊天旁路 graph**（`chat.py:176`）：因 graph 在 interrupt/END 时 `stream` 不可靠。
3. **`StructuredTool.ainvoke` 协程坑**：工具循环里直接 `await` 原始 async 函数（`tools.py`）。
4. **`as_node="critic_node"` 换题技巧**：伪造成从 critic 继续，实现 flush+新题。
5. **多题模式 bug 历史**（已修）：`tutor_messages` dict/Message 混用导致 `AttributeError`；`SessionPhase` 缺 `dialog` 值。现已统一在 API 层转 `Message`。
6. **`run_tool_loop` 动态 `getattr` 解析工具**：使单测能用 `patch` 模块属性替换工具函数，离线全绿。
7. **agent-dialog 与 chat 双入口**：流式（`/chat/stream`）与 `BackgroundTasks` 触发生成，避免 SSE 连接关闭导致题目永不写入。
8. **出题收敛为 Problem Agent**（已合入 `master`）：`problem_generator.py` 退化为 re-export 兼容 shim；`ProblemAgent.generate()`（`agents/agent_problem.py`）按 `LLM→adapter→cli→static` 降级，skill 通道产物经 `_flat_to_problem` 归一为 `Problem`。**注意 `generator_node` 当前并未调用它**（见 §10）。配套流程图见 `problem-agent-flow.md`。

---

## 附录 A：节点依赖图（谁到谁）

```
__start__
  ├─ chat_node ──────────────────────────────► END
  ├─ agent_dialog_node ──(完成)─► planner_node
  │                     └─(未完成)─► END
  └─ planner_node ─┬─(有题)─► wait_for_submit_node
                   └─(无题)─► generator_node ─► wait_for_submit_node
                                          │
                          ┌───────────────┴───────────────┐
                   (普通) judge_node              (agent) agent_judge_node
                          │                                │
                   tutor_router_node                agent_tutor_node
                      ├─RESOLVED─► wait             ├─AC─► END(rev)
                      └─CONT/ESC──► tutor_node      └─WA─► wait(interrupt)
                                   ├─WA──► constitutional_guard ─► END(await)
                                   └─AC/对抗─► update_profile ─► critic
                                                                  ├─ABANDON─► planner
                                                                  ├─AC──────► END(rev)
                                                                  └─WA──────► wait(interrupt)
```

## 附录 B：状态机「暂停/恢复」对照表

| 阶段 | graph 位置 | 触发动作 | 结果 |
|------|-----------|----------|------|
| 生成中 | `wait(interrupt)` PAUSED | 前端轮询 state | 看到 problem |
| 做题中(普通) | `END` 完成 | submit(Command resume) | 重跑→judge→tutor→END |
| 做题中(agent) | `wait(interrupt)` PAUSED | submit(Command resume) | 恢复→agent_judge→agent_tutor→(WA)wait |
| AC 后 | `END(rev)` 完成 | next-problem | critic→planner→generator→wait |
| 辅导对话(agent) | `END(dialog)` 完成 | chat/stream | API 直接 LLM，后台 invoke 出题 |
