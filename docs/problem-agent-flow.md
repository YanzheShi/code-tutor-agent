# 出题 Agent 流程图（Problem Agent）

模块：`src/code_tutor_agent/agents/agent_problem.py`
统一入口：`ProblemAgent(topic, difficulty).generate()` → 返回 `GenerationOutcome(problem, channel, error)`

降级链顺序：**LLM → adapter → cli → static**
任一通道成功立即返回对应 `ProblemChannel`，全部失败返回 `(None, STATIC, error)`。

```mermaid
flowchart TD
    Start([ProblemAgent.generate\ntopic + difficulty]) --> LLM

    %% ───────────── 1) 主通道 LLM ─────────────
    subgraph CH1["① 主通道 · LLM (generate_problem)"]
        direction TB
        L1["get_llm(agnes,\ntemperature=0.7, max_tokens=4096)\nwith_structured_output(Problem)"]
        L2["prompt | structured_llm\nchain.invoke({topic, difficulty})"]
        L3{"重试循环\nattempt <= max_retries?"}
        L4["verify_problem(problem_dict)\n· _extract_code 去围栏\n· 检测思维链泄漏\n· compile(optimal_solution)\n· 从 optimal_solution\n  推导 starter_code / function_signature"]
        L5{"通过校验?"}
        L1 --> L2 --> L3 --> L2
        L2 -->|"调用异常"| L3
        L2 -->|"返回 Problem"| L4 --> L5
        L5 -->|"否"| L3
        L5 -->|"是"| L_OK([返回 Problem · channel=llm])
        L3 -->|"耗尽仍失败\ntopic/difficulty 未出"| L_FAIL(["抛 RuntimeError"])
    end

    Start --> LLM
    LLM --> CH1
    L_OK --> DONE
    L_FAIL --> ADAPTER

    %% ───────────── 2) adapter 通道 ─────────────
    ADAPTER["② adapter 通道\ngenerate_problem_via_skill()\n→ _adapter.generate_problem()"]
    ADAPTER --> A1{"得到 flat dict?"}
    A1 -->|"None / 异常"| CLI
    A1 -->|"dict"| A2["_flat_to_problem(flat)\n补齐 examples/constraints 等必填项"]
    A2 --> A_OK([返回 Problem · channel=adapter])
    A_OK --> DONE

    %% ───────────── 3) CLI 逃生舱 ─────────────
    CLI["③ CLI 逃生舱\ngenerate_problem_via_cli()\n→ run_skill_cli(cta-generate-problem)"]
    CLI --> C1{"r.ok?"}
    C1 -->|"否"| STATIC
    C1 -->|"是"| C2["parse_problem_markdown(r.output)"]
    C2 --> C3{"解析成功?"}
    C3 -->|"None"| STATIC
    C3 -->|"dict"| C4["_flat_to_problem(flat)"]
    C4 --> C_OK([返回 Problem · channel=cli])
    C_OK --> DONE

    %% ───────────── 4) 静态兜底 ─────────────
    STATIC["④ 静态题库兜底\nget_static_problem(topic, difficulty)\n未命中则 get_static_problem()"]
    STATIC --> S1{"得到 flat?"}
    S1 -->|"None"| FAIL([返回 GenerationOutcome\n(None, STATIC, '所有出题通道均失败')])
    S1 -->|"dict"| S2["_flat_to_problem(flat)"]
    S2 --> S_OK([返回 Problem · channel=static])
    S_OK --> DONE

    DONE([GenerationOutcome\nproblem + channel + error])

    %% ───────────── 附：详细题解（独立能力） ─────────────
    SOL["generate_detailed_solution(desc)\n→ _adapter.generate_detailed_solution()"]
    SOL --> SOL1{"成功?"}
    SOL1 -->|"是"| SOLOK([返回题解 markdown])
    SOL1 -->|"异常"| SOLNO([返回 None])

    classDef ok fill:#1f7a3d,stroke:#0c3,color:#fff;
    classDef fail fill:#7a1f1f,stroke:#c33,color:#fff;
    class L_OK,A_OK,C_OK,S_OK,SOLOK ok;
    class L_FAIL,FAIL,SOLNO fail;
```

## 关键说明

| 通道 | 函数 | 通道值 | 失败即降级？ |
| --- | --- | --- | --- |
| 主通道 | `generate_problem` | `llm` | 抛 `RuntimeError` 后降级 |
| skill-engine 进程内 | `generate_problem_via_skill` | `adapter` | 返回 `None` 后降级 |
| skill-engine CLI 逃生舱 | `generate_problem_via_cli` | `cli` | 返回 `None` 后降级 |
| 静态题库 | `static_pool.get_static_problem` | `static` | 返回 `None` 即整体失败 |

- **统一归一**：skill 通道（adapter/cli/static）产物经 `_flat_to_problem` 补齐 `Problem` 必填项后返回。
- **题解能力独立**：`generate_detailed_solution` 走 skill-engine 通道，与上面的出题降级链解耦，失败返回 `None`。
- **Bug7 契约**：LLM 通道用 `max_tokens=4096` 限流、重试耗尽抛 `RuntimeError`（不静默返回 `None`）。
