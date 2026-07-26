# 设计文档：skill-engine CLI 逃生舱接入

> 分支：`master`（CLI 逃生舱功能已并入主干；本文件为**历史设计文档**）
> 架构定位已被 `docs/skill-engine-adapter-design.md` 取代：CLI 不再是"备选/扩展通道"，
> 而是**显式 opt-in 的逃生舱**；主通道为进程内 `engine_adapter` import 通道。
> 关联文档：`docs/skill-engine-adapter-design.md`（主架构）、`docs/system-architecture-and-flow.md`
> 配套项目：`D:/Code/PycharmProjects/skill-engine`（独立 typer CLI，核心命令 `skill-engine run <skill> -a "<args>" --llm`）

> ⚠️ **历史文档**：本文档写于"subprocess CLI 当备选通道"的旧定位阶段，现已**被适配器设计文档取代**，
> 仅保留 §2（工具底座约束）、§7（失败降级语义）、§10（测试 mock 套路）作为可参考的落地细节。
> CLI 逃生舱当前**默认不启用**，仅当用户显式 `mode="cli"` / 调试实验 skill / 复现 CI 时才走
> （白名单仅 `cta-generate-problem` + `cta-generate-solution`）。

---

## 0. 背景：为什么接了 tool calling 后这步更简单

tool-calling 落地后，`src/code_tutor_agent/agents/tools.py` 里已沉淀出一套**可复用的工具执行底座**：

- `StructuredTool.from_function` 把本地函数包成工具；
- `run_tool_loop(llm, messages, tools, max_rounds)`：LLM 自主决定调工具 → `getattr(_self_module, name)` 解析函数 → `await fn(**args)` → 异常统一转 `{"error": ...}` → `max_rounds` 防空转 → `allowed` 白名单校验；
- 同步调用统一 `asyncio.to_thread` 包一层防阻塞事件循环。

**CLI 逃生舱本质就是「再加一个跑 `subprocess` 的 `StructuredTool`」，直接塞进这套现成底座**，不需要新造任何编排/循环/错误处理逻辑。从零搭通道 → 退化成"填一个函数"。

---

## 1. 通道定位：出题的三条链路与降级链

| 优先级 | 通道 | 方式 | 角色 |
|---|---|---|---|
| ① | 进程内直连 | `generator_node` 直接调本系统 LLM（`agents/problem_generator.generate_problem`） | **主通道** |
| ② | **CLI 逃生舱（本次接）** | `subprocess` spawn `skill-engine run cta-generate-problem -a ... --llm` | 备选 / 扩展通道 |
| ③ | 静态题库 | `store/static_pool.get_static_problem` | 最后兜底 |

**"逃生舱"含义**：当主通道（进程内 `engine_adapter` import 通道，见 `docs/skill-engine-adapter-design.md`）不可用，或想绕开进程内逻辑、改走独立 CLI 进程执行（`cta-generate-problem` / `cta-generate-solution` 两个白名单内 skill）时使用，带来进程隔离、可独立迭代的好处。

> 注：旧方案曾规划多题型 domain skill（`cta-generate-math` / `cta-generate-sci-comp` / `cta-generate-engineering` / `cta-generate-ai`），但**当前 `master` 分支的 `skills/defs/` 下并不存在这些 def**，白名单与运行期仅含 `cta-generate-problem` + `cta-generate-solution`；`cta-generate-test-cases` 为独立新资产、未接入 CLI 白名单。请勿据此文档假设多题型 skill 已落地。

> **待拍板点（影响 §5 接入方式）**：CLI 通道默认当**降级/备选**还是**主通道**？
> - 本文档默认按"备选 + 多题型场景由对话 LLM 自主选"设计（更稳）。
> - 若意图是"以后出题主要走 skill-engine CLI"，则 §5 的 `generator_node` 接线要反转：CLI 为首选、进程内直连变兜底。

---

## 2. 核心约束（来自现有底座，必须遵守）

1. **函数解析位置**：`run_tool_loop` 用 `getattr(_self_module, name)` 只从 `tools.py` 模块内解析函数名（`tools.py:208`）。因此 CLI 工具的 **`async` 包装函数必须定义在 `tools.py`**（与现有 `parse_leetcode` / `judge_*` 同款），而**同步核心放在新文件 `agents/skill_cli.py`** 被 `tools.py` import。测试时用 `patch("code_tutor_agent.agents.tools.run_skill_cli", ...)` 离线 mock。
2. **工具注册**：`get_tool()` 只遍历 `AGENT_TOOLS`（`tools.py:156`）。新增 CLI 工具单独组织成 `SKILL_TOOLS` 列表，按需传给 `run_tool_loop(tools=SKILL_TOOLS)`，不建议无条件塞进 `AGENT_TOOLS`（避免辅导环节误暴露出题工具）。
3. **白名单校验**：`run_tool_loop` 已有 `allowed` 层（仅执行 `tools` 参数里的工具名）。但 CLI 逃生舱要**额外**在 runner 内部校验 `skill_name ∈ SKILL_ENGINE_CLI_ALLOWLIST`——因为 skill 名是拼进 `subprocess` 的参数，不是 LLM 直接选的函数名，必须防注入。

---

## 3. 改动一：`config.py` 新增配置

在 `config.py` 末尾追加（沿用现有 `get_*` + `os.getenv` 风格）：

```python
# ── skill-engine CLI 逃生舱 ──
import os as _os

def get_skill_engine_dir() -> str:
    """skill-engine 项目根目录（含 skills/ 子目录）。

    从环境变量 SKILL_ENGINE_DIR 读取，默认 D:/Code/PycharmProjects/skill-engine。
    run 命令用 Path.cwd()/skills 扫描 skill，因此 spawn 时必须 cwd 指到这里。
    """
    return _os.getenv(
        "SKILL_ENGINE_DIR",
        "D:/Code/PycharmProjects/skill-engine",
    )

def get_skill_engine_cli_timeout() -> int:
    """CLI 子进程超时（秒），默认 60。"""
    return int(_os.getenv("SKILL_ENGINE_CLI_TIMEOUT", "60"))

#: 允许通过 CLI 执行的 skill 名白名单（防止任意 skill 名注进 subprocess）
SKILL_ENGINE_CLI_ALLOWLIST: frozenset[str] = frozenset(
    _os.getenv(
        "SKILL_ENGINE_CLI_ALLOWLIST",
        "cta-generate-problem,cta-generate-solution",
    ).split(",")
)
```

**约 25 行。**

---

## 4. 改动二（核心新文件）：`agents/skill_cli.py`

唯一 spawn `skill-engine` 的"门神" + Markdown 契约解析器。

### 4.1 `run_skill_cli`

```python
"""skill-engine CLI 逃生舱：subprocess 执行 skill 并收结果。

踩坑备忘（已规避）：
- Windows GBK 解码崩溃：Popen 必须 encoding="utf-8", errors="replace"，
  out/err 加 or "" 防御。
- skill 扫描依赖 cwd：spawn 时 cwd 必须指向 skill-engine 根目录。
- 中文 argv 乱码：PowerShell 直接塞中文到 argv 会乱码（环境编码问题，非 bug）。
  故一律用 list 形式传参（不走 shell 字符串拼接）；topic 优先用英文键，
  必要时改用 --args-file 临时文件/环境变量传参彻底绕开 argv 编码。
"""
from __future__ import annotations
import json
import subprocess
from code_tutor_agent.config import (
    get_skill_engine_dir,
    get_skill_engine_cli_timeout,
    SKILL_ENGINE_CLI_ALLOWLIST,
)

def run_skill_cli(
    skill_name: str,
    args: dict,
    *,
    use_llm: bool = True,
    timeout: int | None = None,
) -> dict:
    """执行一个 skill-engine skill，返回结构化结果。

    Args:
        skill_name: skill 名，必须在 SKILL_ENGINE_CLI_ALLOWLIST 内。
        args: skill 占位符字典（如 {"topic": "数组", "difficulty": "easy"}）。
        use_llm: 是否带 --llm（走 LLM 生成）；False 走 dry-run 模板。
        timeout: 子进程超时（秒），默认读配置。

    Returns:
        {
          "ok": bool,
          "exit_code": int,
          "stdout": str,
          "stderr": str,
          "skill_name": str,
          "error": str | None,   # ok=False 时填原因
        }
    """
    if skill_name not in SKILL_ENGINE_CLI_ALLOWLIST:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "",
                "skill_name": skill_name,
                "error": f"skill '{skill_name}' 不在白名单 {sorted(SKILL_ENGINE_CLI_ALLOWLIST)}"}

    timeout = timeout or get_skill_engine_cli_timeout()
    # -a 参数序列化：skill 约定形如 topic=数组,difficulty=easy
    args_str = ",".join(f"{k}={v}" for k, v in args.items())
    cmd = ["skill-engine", "run", skill_name, "-a", args_str]
    if use_llm:
        cmd.append("--llm")

    try:
        proc = subprocess.run(
            cmd,
            cwd=get_skill_engine_dir(),          # 关键：否则扫不到 skills/
            capture_output=True,
            encoding="utf-8", errors="replace",  # 关键：防 Windows GBK 崩
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "",
                "skill_name": skill_name, "error": f"超时（>{timeout}s）"}
    except FileNotFoundError:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "",
                "skill_name": skill_name, "error": "skill-engine 命令未找到（SKILL_ENGINE_DIR 配错？）"}

    out = proc.stdout or ""
    err = proc.stderr or ""
    ok = proc.returncode == 0 and bool(out.strip())
    return {
        "ok": ok,
        "exit_code": proc.returncode,
        "stdout": out,
        "stderr": err,
        "skill_name": skill_name,
        "error": None if ok else (err.strip() or f"exit_code={proc.returncode}"),
    }
```

### 4.2 Markdown 契约解析器

`skill-engine run` 默认打印人类可读文本（带 `====` 分隔 + `result["output"]`）。从 stdout 抠出 `cta-generate-problem` 的契约节，转成本系统结构。

```python
import re

# cta-generate-problem 输出契约节（## Title / Topic / Difficulty / Description /
# Examples / Constraints / StarterCode / BruteSolution / OptimalSolution）
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

def parse_problem_markdown(stdout: str) -> dict | None:
    """从 skill 输出里抠出契约节 → 扁平 dict。

    返回 None 表示解析不到任何契约节（stdout 异常）。
    字段映射严格对应 ProblemMeta（见 schemas/state.py:34）。
    """
    if not stdout or "##" not in stdout:
        return None
    # 取最后一个 "====" 之后的 body（屏蔽 run 命令自身的前缀日志）
    body = stdout.split("====")[-1] if "====" in stdout else stdout
    parts = _SECTION_RE.split(body)
    # parts: ['', 'Title', '...', 'Topic', '...', ...]
    if len(parts) < 3:
        return None
    sections: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        sections[parts[i].strip().lower()] = parts[i + 1].strip()

    examples_raw = sections.get("examples", "")
    sample_tcs = _parse_examples_to_test_cases(examples_raw, "")  # 复用现有解析

    return {
        "title": sections.get("title", "Untitled"),
        "topic": sections.get("topic", "数组"),
        "difficulty": (sections.get("difficulty", "easy") or "easy").lower(),
        "description": sections.get("description", ""),
        "starter_code": _strip_fences(sections.get("startercode", "")),
        "optimal_solution": _strip_fences(sections.get("optimalsolution", "")
                                          or sections.get("brutesolution", "")),
        "test_cases": sample_tcs,
    }

def _strip_fences(s: str) -> str:
    m = re.search(r"```[a-zA-Z]*\n?(.*?)```", s, re.DOTALL)
    return m.group(1).strip() if m else s.strip()
```

**约 110 行（含文档注释）。**

---

## 5. 改动三：`agents/tools.py` 接入工具（复用 `run_tool_loop`）

在 `tools.py` 末尾追加。async 包装落在 `tools.py` 以满足 §2 约束，sync 核心来自 `skill_cli`。

```python
from code_tutor_agent.agents.skill_cli import run_skill_cli

# ──────────────────────────────────────────────
#  skill-engine CLI 逃生舱
# ──────────────────────────────────────────────

def _generate_problem_via_skill_sync(topic: str, difficulty: str) -> str:
    """Synchronous core: 通过 CLI 逃生舱出题，返回 JSON 字符串。"""
    r = run_skill_cli("cta-generate-problem", {"topic": topic, "difficulty": difficulty})
    if not r["ok"]:
        return json.dumps({"error": f"CLI 出题失败: {r['error']}"}, ensure_ascii=False)
    parsed = parse_problem_markdown(r["stdout"])
    if parsed is None:
        return json.dumps({"error": "CLI 出题成功但契约解析失败"}, ensure_ascii=False)
    return json.dumps(parsed, ensure_ascii=False)


async def generate_problem_via_skill(topic: str, difficulty: str) -> str:
    """通过 skill-engine CLI 逃生舱生成题目（备选出题通道）。"""
    return await asyncio.to_thread(_generate_problem_via_skill_sync, topic, difficulty)


# 仅在「对话/需求澄清阶段」由 LLM 自主选择题型时使用
SKILL_TOOLS = [
    StructuredTool.from_function(
        func=generate_problem_via_skill,
        name="generate_problem_via_skill",
        description=(
            "通过 skill-engine CLI 逃生舱生成练习题。当用户在对话中明确要求"
            "用 skill-engine 出题、或点名某类题型（数学推导/科学计算/工程场景/AI 启发式）时调用。"
            "参数 topic 与 difficulty 由对话上下文决定。普通 LeetCode 风格出题不要用此工具。"
        ),
    ),
]
```

> 可选：若希望 CLI 工具在全局可用，把 `SKILL_TOOLS` 元素 `+=` 进 `AGENT_TOOLS`。但默认建议仅在对话 LLM 的 `run_tool_loop(tools=SKILL_TOOLS)` 中暴露。

**约 35 行。**

---

## 6. 改动四：`generator_node` 接降级链（确定性出题路径）

> ⚠️ **本节的 `generator_node` 接线方案未被采纳（历史设计）**：实际主干里的 `nodes/generator.py::generator_node`
> 走的是**内联降级** `LLM → adapter（engine_adapter.generate_problem）→ static`，**没有 cli 通道、也不调用 `ProblemAgent`**
> （详见 `docs/system-architecture-and-flow.md` §10 与 `docs/problem-agent-flow.md`）。即 CLI 逃生舱在主干中仅为**显式 opt-in**
> （用户 `mode="cli"` / 调试 / 复现 CI 时走），并未按本节接进 `generator_node` 的默认降级链。

`nodes/generator.py` 现有结构：

- 路径 A：`state.leetcode` 非空 → LeetCode 导入（跳过 LLM）
- 路径 B：`for attempt in range(1, MAX_ATTEMPTS+1)` 调 `generate_problem(topic, difficulty)` → 解析示例 → 跑参考解拿期望输出 → 成功则 `break`
- `else`（路径 B 全失败）→ 静态池兜底

**插入 CLI 作为路径 B 与静态池之间的降级层**（对应 §1 的"主通道失败→逃生舱→静态池"）：

```python
# generator_node 路径 B 的 for 循环后、静态池兜底前，新增：
else:
    # ── 路径 C：CLI 逃生舱（LLM 主通道全失败后才走）──
    logger.warning("LLM 出题 %d 次失败 — 尝试 CLI 逃生舱", MAX_ATTEMPTS)
    _progress(sid, "⚠️ 进程内出题失败，尝试 CLI 逃生舱…")
    writer("⚠️ 进程内出题失败，尝试 CLI 逃生舱…")
    cli_json = await run_skill_cli_and_parse(topic, difficulty)  # 包一层 async
    if cli_json:
        problem_dict = cli_json
        sample_tcs = cli_json.get("test_cases", [])[:2]
        # 复用下方「持久化到 DB」+ ProblemMeta 构造逻辑（无需改）
    else:
        # ── 路径 D：静态池（原 else 内的逻辑整体下移）──
        logger.warning("CLI 逃生舱也失败 — 回退静态题库")
        _progress(sid, "⚠️ CLI 失败，切换到静态题库…")
        problem_dict = get_static_problem(topic=topic, difficulty=difficulty) \
            or get_static_problem()
        sample_tcs = problem_dict.get("test_cases", [])[:2]
```

> 注：`generator_node` 当前是**同步**函数（`def generator_node(...)`），而 `run_skill_cli` 是同步核心。可直接在节点内 `run_skill_cli(...)`（不进事件循环），或 `asyncio.to_thread` 包一层。需把节点改成 `async def` 或就地同步调用——见 §8 落地步骤的注意点。

**约 30 行（复用节点现有 DB 持久化 / ProblemMeta 构造，不重复写）。**

---

## 7. 失败处理与降级语义

| 环节 | 失败条件 | 行为 |
|---|---|---|
| `run_skill_cli` 内部 | skill 不在白名单 / 命令不存在 / 超时 | 返回 `{"ok": False, "error": ...}`，**不抛异常** |
| `_generate_problem_via_skill_sync` | `ok=False` 或契约解析为 `None` | 转 `{"error": ...}` JSON |
| `run_tool_loop` | 工具抛异常 | 转 `{"error": ...}` JSON（已有逻辑） |
| `generator_node` | CLI 整体失败 | 下钻到静态池兜底（§6 路径 D） |

**原则**：任何一层失败都降级而非冒泡，绝不打断会话。

---

## 8. 落地顺序（每步可独立验证）

1. **`config.py`**：加 §3 配置 → 单测读默认值。
2. **`agents/skill_cli.py`**：`run_skill_cli` + `parse_problem_markdown` → 用假 stdout（`patch subprocess.run`）离线验证 parser → `ProblemMeta` 映射。
3. **`agents/tools.py`**：追加 `generate_problem_via_skill` + `SKILL_TOOLS` → 复用现有 `run_tool_loop` 动态 `getattr`，`patch run_skill_cli` 单测。
4. **`generator_node`**：接 §6 降级链（CLI 为第二通道）；对话阶段按需 `bind_tools(SKILL_TOOLS)` 走 `run_tool_loop`。
5. **（可选）真实冒烟**：需 skill-engine 环境，实跑 `cta-generate-problem` 验证端到端。

**注意点**：`generator_node` 现为同步 `def`，接入 CLI 时若节点内直接 `run_skill_cli(...)` 同步阻塞可接受（本就同步调 `generate_problem`）；若改用 `asyncio.to_thread` 则需节点改 `async def`。建议保持同步调用，改动最小。

---

## 9. 改动量估算

| 文件 | 动作 | 行数 |
|---|---|---|
| `config.py` | 改 | ~25 |
| `agents/skill_cli.py` | **新** | ~110 |
| `agents/tools.py` | 改 | ~35 |
| `nodes/generator.py` | 改 | ~30 |
| `tests/test_skill_cli.py` | **新** | ~100 |
| `tests/test_agent_tools.py` | 改 | ~30 |

**合计 ≈ 330 行**，4 改 + 2 新。真正的新逻辑集中在 `skill_cli.py`（subprocess 封装 + 契约解析），其余全部复用现有 `run_tool_loop` / `generator_node` 的持久化与 `ProblemMeta` 构造。

---

## 10. 测试策略

- **runner 层**：`unittest.mock.patch("subprocess.run")`，喂一段契约 Markdown 的假 stdout，断言 `parse_problem_markdown` → 字段齐全且映射正确；断言中文乱码/空 stdout/非零 exit 三种失败都被转成 `{"ok": False}`。
- **工具层**：`patch("code_tutor_agent.agents.tools.run_skill_cli", return_value={...})`，验证 `generate_problem_via_skill` 经 `run_tool_loop` 被 LLM 选中并拿到结果（同现有 judge 工具测试套路，离线全绿）。
- **节点层**：`patch` `run_skill_cli` 模拟"主通道失败→CLI 成功"与"CLI 也失败→静态池"，断言降级链路正确、不抛异常。
- **端到端（可选）**：在装有 skill-engine 的环境实跑一条 `cta-generate-problem`，核对契约节与 `ProblemMeta` 一致性。
