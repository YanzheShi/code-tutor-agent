# 设计文档：skill-engine 接入（主通道 = 进程内 import）

> 分支：`feat/skill-engine-cli`（本文档为该分支的**目标架构**设计，取代旧文档
> `docs/skill-engine-cli-escape-hatch-design.md` 中"仅 subprocess 逃生舱"的定位判断）
> 配套项目：`D:/Code/PycharmProjects/skill-engine`（独立 typer CLI / 可 import 的 Python 包）
> 参考：`docs/skill-engine-integration-design.md`（`feat/cli` 分支的设计，本文档 §4/§6/§10 复用其结构）

---

## 1. 决策摘要：为什么把 import 通道抬成主通道

上一轮架构讨论的结论（已核对真实代码与踩坑记录）：

| 维度 | subprocess CLI（当前 `skill_cli.py`） | 进程内 import（新增 `engine_adapter`） |
|---|---|---|
| 调用方式 | `subprocess.run(["skill-engine","run",...])` | `from skill_engine import discover, Registry, ...` |
| 返回 | 文本 stdout，需再解析 | 结构化 `dict`（`result["output"]`） |
| 环境耦合面 | PATH / cwd / argv 编码 / GBK（**运行时才炸**） | 仅"同 venv 装了 skill_engine"（**导入即可静态发现**） |
| 代码量（本系统） | ~166 行半数在填 subprocess 的坑 | ~120 行，无编码/进程树/截断逻辑 |
| 测试 | mock `subprocess.run` + 伪造 stdout/exit | mock 一个函数即可 |
| Linux 部署 | 仍需保证裸命令进 PATH | 同 venv 依赖，零环境坑 |

**结论**：对"两个自有 Python 项目 + 要上 Linux"的场景，进程内 import 在
耦合稳定性、代码简洁度、可测性、部署可靠性四个维度都更优。subprocess CLI 保留，
但**降级为显式 opt-in 的逃生舱**（调试新 skill / 跑未知 skill / 用户聊天里显式喊
"用 CLI 跑 xxx" / 要 1:1 复现 CI 行为），**不再是默认通道**。

> 一句话定位：
> - **import 通道 = 生产主通道**（高频、结构化、落库、低延迟）。
> - **subprocess 通道 = 逃生舱**（opt-in 调试 / 隔离实验 / 复现 CI）。

### 1.1 已拍板的决策点（讨论结论）

| ID | 决策 | 结论 | 影响 |
|---|---|---|---|
| DP-1 | CLI 逃生舱 | **保留为 opt-in 逃生舱**（双通道：import 主 + subprocess 逃生舱） | 隔离/复现 CI 能力保留；维护两套护栏但 parser 合并（见 DP-5） |
| DP-2 | adapter 范围 | **只做 `cta-generate-problem` + `cta-generate-solution`**；`generate_problem` 接口预留 `skill_name` 参数，为多题型（math/sci/eng/ai）留口子，本次不实现 | 范围收敛、易验收 |
| DP-3 | 专属 skill 归属 | **放本仓 `src/code_tutor_agent/skills/defs/`**，随仓发布；`get_skill_engine_skills_root` 默认指向此处，`discover(roots=[本仓绝对路径])` | 版本随主仓走，部署简单 |
| DP-4 | 白名单 | **精简为两个真实 def**：`cta-generate-problem` + `cta-generate-solution`（即详细题解 skill，feat/cli 命名）；**移除幽灵名 `cta-generate-solution`**（当前 `skill_cli.py` 引用了它但无对应 def，落地时一并改正） | `config.SKILL_ENGINE_CLI_ALLOWLIST` 默认值收紧 |
| DP-5 | 双通道 parser | **抽共享解析模块 `skills/parser.py`**，adapter 与 cli 均调用，从根上消除输出结构漂移 | 取消"各自 parser + 契约测试兜底"方案；契约测试改为验证共享 parser 行为 |

---

## 2. 文档目的与范围

本文档定义把 `skill-engine` 接进 code-tutor-agent 的**目标架构、模块边界、接口契约、
配置、可观测性、测试与实施路线**，是后续实现的唯一依据。

**In scope**
- `skill-engine` 作为同一 venv 的依赖接入（dev 用 editable，prod 用 git tag / 锁定版本）。
- 两条调用通道：**主通道（库内 import `engine_adapter`）** + **逃生舱（subprocess `skill_cli`）**。
- 适配层、CLI 逃生舱、LangGraph tool（路由）、`generator_node` 兜底、可观测、测试、CI 回归。
- 配置单一真源、版本升级 SOP。

**Out of scope**
- 修改 `skill-engine` 自身代码（假定其为完整、无 bug 的通用执行器）。
- 把 `skill-engine` 改造成 MCP server（当前规模不需要，演进见 §14）。
- 重新设计已有的出题/解题业务模型（复用 `ProblemMeta` 与 `generator_node` 落库链路）。

---

## 3. 系统定位与约束（已核对真实代码）

| 组件 | 真实位置 | 与本次接入的关系 |
|---|---|---|
| 出题热路径 | `nodes/generator.py`：路径 A（LeetCode）/ 路径 B（进程内 LLM `generate_problem`）/ 路径 C（CLI 兜底）/ 路径 D（静态池） | 路径 C 改走 `engine_adapter.generate_problem` |
| Tutor 对话 Agent | `agents/agent_dialog.py`（裸 LLM + 结构化输出，无 tool 循环） | 出题前对话不绑 skill 工具（旧文档 §agent_dialog:438 已说明） |
| 辅导环节工具 | `agents/tools.py` 的 `TUTOR_TOOLS`（`judge_*` + `generate_detailed_solution_via_skill`） | 详细题解工具改走 `engine_adapter` 或 opt-in CLI |
| 工具底座 | `agents/tools.py`：`run_tool_loop` + `getattr(_self_module, name)` + `asyncio.to_thread` | 新增工具函数继续落 `tools.py`，sync 核心落 `engine_adapter`/`skill_cli` |
| LLM 配置 | `config.py`：`get_llm(alias)` | adapter 通道的 LLM 单一真源 |
| 领域模型 | `ProblemMeta`（`schemas/state.py`）+ `db` 落库 | adapter 输出归一目标 |
| 现有契约解析 | `agents/skill_cli.parse_problem_markdown` | 解析逻辑平移进 `engine_adapter`，行为保持一致 |

**关键约束**
- 单进程常驻 FastAPI；热路径要低延迟、要结构化结果 → 走 import 通道。
- 进程内长调用必须 `asyncio.to_thread` 包一层，避免阻塞事件循环（现有 `tools.py` 已如此）。
- 聊天 / CI / 运维行为必须一致（单一事实来源）。
- 任何一层失败都降级而非冒泡，绝不打断会话（沿用旧文档 §7 原则）。

---

## 4. 整体架构

```
                    code-tutor-agent (FastAPI 常驻)
   ┌────────────────────────────────────────────────────────────────┐
   │ 入口类型图例:                                                    │
   │   [自动]  = 服务端代码 try/except 触发，无 LLM 参与(非 tool calling)│
   │   [tool] = LLM 经 tools.py 工具调用触发（由 mode/channel 选通道）│
   │   (opt-in)= 需显式 mode/channel="cli" 才走，否则不触发          │
   │                                                                    │
   │  出题热路径 (nodes/generator.py)  ─────────── [自动] ──────────  │
   │    路径 B 主通道: 进程内 LLM generate_problem                      │
   │       └─ 失败 → 路径 C [自动]:                                   │
   │            skills.engine_adapter.generate_problem(...)  ─import─▶   │
   │              skill_engine.Runner (discover/Registry/ 构造MatchResult)│
   │            → 归一 Problem → 复用落库/SSE 链路                       │
   │       → 仍失败 → 路径 D: 静态池                                    │
   │                                                                    │
   │  Tutor 辅导 (agents/tools.py::TUTOR_TOOLS)  ───── [tool] ──────  │
   │    generate_detailed_solution_via_skill():                          │
   │      默认 → engine_adapter.run_skill(cta-generate-solution)│
   │      mode="cli" (opt-in) → skill_cli.run_skill_cli (subprocess)   │
   │                                                                    │
   │  对话意图 (SKILL_TOOLS / generate_problem_via_skill)  ─ [tool] ── │
   │      默认 → engine_adapter.generate_problem                          │
   │      mode="cli" (opt-in) → skill_cli.run_skill_cli                 │
   │                                                                    │
   │  Admin / 运维 (可选 POST /admin/skill/run)  ── [tool/手动] ───── │
   │      channel=adapter 默认；channel=cli (opt-in) 走逃生舱           │
   └────────────────────────────────────────────────────────────────┘
                         │ (逃生舱 opt-in, 仅当 mode/channel="cli")
        ┌────────────────┴─────────────────┐
        ▼                                  ▼
  skill-engine CLI (人工调试)        CI: scripts/skill_smoke.py (subprocess 同形回归)
```

> **入口澄清（避免"两者都靠 tool calling"的误解）**
> - **engine_adapter 主用法是 `[自动]`**：路径 C 兜底由 `generator.py` 的 `try/except` 直接调用，
>   **与 tool calling 无关**，即使没有任何 LLM 工具调用也照常工作。
> - **engine_adapter 的 `[tool]` 入口**（`tools.py` 工具，默认 `mode="adapter"`）是另一条可选项，非生产必需。
> - **skill_cli 仅 `[tool]`(opt-in) 或运维脚本**触发：只在 LLM 显式 `mode="cli"`（或 Admin `channel=cli`）时走，
>   **不在路径 C 自动路径里**。
> - LLM **不直接**调 `engine_adapter`/`skill_cli`；它调 `tools.py` 封装函数，由 `mode` 决定通道。

**双通道取舍规则（明确，避免滥用）**
- 走 **engine_adapter（import）**：已知、稳定、高频、要结构化/落库（出题、`generate_problem`、详细题解默认）。
- 走 **skill_cli（subprocess）**：① 显式 `mode="cli"`；② skill 在「实验白名单」；
  ③ 用户聊天里显式喊"用 CLI 跑 xxx skill"；④ 要 1:1 复现 CI 行为。
- 即 subprocess 是**显式 opt-in 的逃生舱，不是默认通道**（与旧文档默认"备选"定位不同）。
- **入口类型提醒**：`engine_adapter` 的生产主路径是 `generator.py` 路径 C 的 `[自动]` 兜底（非 tool calling）；
  而 `skill_cli` 只在 `tools.py` 工具 `mode="cli"`（或 Admin `channel=cli`）的 `[tool]`/运维入口触发。
  "CLI vs adapter 的区分"仅发生在 `tools.py` 这一层（靠 `mode`），不要误认为两者都靠 tool calling。

---

## 5. 依赖与版本策略

### 5.1 本地开发（当前状态）
`skill-engine` 是本地兄弟仓库，已通过 editable 装进 **code-tutor-agent 的 venv**：

```powershell
cd D:/Code/PycharmProjects/code-tutor-agent
uv pip install -e D:/Code/PycharmProjects/skill-engine
uv run skill-engine --help      # 验证：同 venv 内可找到，subprocess 与 import 双通道才都通
```

> ⚠️ 必须装进 **code-tutor-agent 的 venv**，不是 skill-engine 自己的 venv。
> 否则 import 通道 `import skill_engine` 直接 `ModuleNotFoundError`，subprocess 通道
> 也因为裸命令名 `skill-engine` 不在主进程 PATH 而 `FileNotFoundError`。

### 5.2 生产 / Linux 部署
两种稳妥方案（不依赖裸命令名 + editable）：

1. **作为同一镜像/venv 的正式依赖**（推荐）
   - `pyproject.toml` 加 git tag 依赖（参考 `feat/cli` 文档 §5.1），`uv lock --locked` 锁版本；
   - Docker 内与主服务同环境安装，import 通道天然可用，subprocess 通道命令也必在 PATH。
2. **源码包 import（本设计的主通道）**——本身就是为规避 PATH/编码/ cwd 而生，
   生产环境最省心：**没有 PATH 依赖、没有子进程编码坑、没有 CLI 找不到的问题**。

### 5.3 升级 SOP
1. 在 `skill-engine` 仓库打新 tag；2. 改 `pyproject.toml` tag → `uv lock`；
3. 跑契约测试（§12.2）+ CI 全量回归；4. 通过合入，否则在适配层做兼容改动后重测。
> 因无 PyPI semver 自动上界，tag 纪律替代 semver 约束，**契约测试是兼容保障核心**（§12）。

---

## 6. 适配层设计（engine_adapter，主通道）

> 新文件：`src/code_tutor_agent/skills/__init__.py`
> 新文件：`src/code_tutor_agent/skills/engine_adapter.py`

### 6.1 职责
1. **全系统唯一 `import skill_engine` 的地方**（依赖隔离；CLI 逃生舱 `skill_cli` 不 import）。
2. 确定性 Bootstrap：经 `discover(roots=[本仓 defs 绝对路径])` 指入本系统专属 skill，
   直接 `load_skill` + 构造 `MatchResult`（**绕过 Router**，省一次 LLM 路由、杜绝匹配错 skill），
   用本系统 LLM 跑。
3. 把 skill 输出的**确定性分节 markdown** 解析为 `ProblemMeta` 字段 / 题解 dict / 原始 markdown。
4. 模板变量（`{topic}`/`{difficulty}`/`$ARGUMENTS`）完全交给 skill-engine 解析，adapter 不做替换。
5. 统一错误语义（`SkillError` / `SkillConfigError` / `SkillExecutionError`）。

### 6.2 对外接口（实现保持一致）

```python
# skills/engine_adapter.py
from code_tutor_agent.schemas.state import ProblemMeta  # 或 models.problem.Problem

class SkillError(Exception): ...
class SkillConfigError(SkillError): ...     # 参数/配置问题（4xx 语义）
class SkillExecutionError(SkillError): ...   # 引擎内部执行/解析失败（5xx 语义）

@dataclass
class SkillResult:
    skill_name: str
    ok: bool
    output: str                  # 主文本输出（markdown）
    artifacts: list[dict] = ...
    meta: dict = ...
    error: str | None = None

DEFAULT_PROBLEM_SKILL = "cta-generate-problem"
DEFAULT_DETAILED_SOLUTION_SKILL = "cta-generate-solution"
# DP-2：本次仅实现 problem + detailed-solution；generate_problem 预留 skill_name 形参，
# 为多题型（math/sci/eng/ai）留口子，本次不接。

def run_skill(
    skill_name: str,
    *,
    arguments: dict[str, str] | None = None,   # 注入 skill 模板的键值
    llm_alias: str | None = None,              # 默认取 get_skill_engine_llm_alias()
) -> SkillResult:
    """通用 skill 运行入口（import 通道，确定性）；成功返回 ok=True 与 markdown，
    任何错误归一为 ok=False（error 字段），不向外抛。"""

def generate_problem(
    topic: str, difficulty: str,
    *, skill_name: str = DEFAULT_PROBLEM_SKILL,
    llm_alias: str | None = None, max_retries: int = 1,
) -> ProblemMeta:
    """出题：跑 skill → 解析为 ProblemMeta → 复用现有校验网关。
    失败时带原因重跑，重试耗尽抛 SkillExecutionError。"""

def generate_detailed_solution(
    problem_description: str,
    *, skill_name: str = DEFAULT_DETAILED_SOLUTION_SKILL,
    llm_alias: str | None = None,
) -> str:
    """详细题解：给定整题 description，返回 markdown 文本（不解析进结构）。"""
```

### 6.3 确定性 Bootstrap（绕过 Router）

> ✅ **Phase 0 已核对（2026-07-18）**：下列为当前 `skill-engine` 版本的真实导出，
> import 路径与 `feat/cli` 不同——`discovery/registry/router` 在 `skill_engine.routing` 下，
> `executor/assembler/runner` 在 `skill_engine.execution` 下；`MatchResult(skill, score, method, arguments)`
> 签名已确认；`Runner.run(match_result, llm=...)` 走档位 A 单次 LLM 调用，返回 `result["output"]`。
> 完整 in-process 管线已用假 LLM 跑通验证（见 Phase 0 验收记录）。

```python
def _bootstrap(skill_name: str, arguments: dict, llm_alias: str | None) -> str:
    from skill_engine.routing.discovery import discover
    from skill_engine.routing.registry import Registry
    from skill_engine.models import MatchResult
    from skill_engine.execution.executor import Executor
    from skill_engine.execution.assembler import Assembler
    from skill_engine.execution.runner import Runner

    skills_root = get_skill_engine_skills_root()            # 本仓 defs 绝对路径
    index = discover(roots=[skills_root])                   # 绝对路径，不经 cwd
    if skill_name not in index:
        raise SkillConfigError(f"skill 未找到: {skill_name}")
    registry = Registry(index)
    skill = registry.load_skill(skill_name)
    if not skill:
        raise SkillConfigError(f"skill 加载失败: {skill_name}")

    alias = llm_alias or get_skill_engine_llm_alias()
    llm = get_llm(alias, temperature=0.7)                  # 本系统 LLM 单一真源

    mr = MatchResult(
        skill=skill, score=1.0, method="exact",
        arguments={"$ARGUMENTS": arguments.get("$ARGUMENTS", ""),
                   "$0": arguments.get("$0", ""),
                   **{k: v for k, v in arguments.items() if not k.startswith("$")}},
    )
    executor = Executor(timeout=60, allow_all=True)
    assembler = Assembler(executor=executor, command_timeout=60)
    runner = Runner(assembler, executor)
    result = runner.run(mr, llm=llm)                      # 返回 dict: output/...
    output = (result.get("output") or "").strip()
    if result.get("error"):
        raise SkillExecutionError(f"skill 返回错误: {result['error']}")
    if not output:
        raise SkillExecutionError("skill 输出为空")
    return output
```

### 6.4 输出契约与解析（共享解析模块，保持与旧 `parse_problem_markdown` 行为一致）

> 见 **DP-5**：解析逻辑从 `skill_cli.parse_problem_markdown` 抽出为**共享模块**
> `src/code_tutor_agent/skills/parser.py`（`parse_problem_markdown` / `_strip_fences` /
> `_parse_examples_to_test_cases`），adapter 与 cli 均 import 它，杜绝双通道漂移。

**`cta-generate-problem` 输出契约**（skill 必须严格按此分节；当前分支实测已含全部节）：

```
## Title
<题目标题>
## Topic
<知识点>
## Difficulty
easy|medium|hard
## Description
<含背景/输入输出定义/示例的完整描述 markdown>
## Examples
Example1:
Input: ...
Output: ...
## Constraints
- <约束1>
## StarterCode
```python
class Solution:
    def method(self, ...): ...
```
## BruteSolution
```python
class Solution:
    def solve(self, ...): ...
```
## OptimalSolution
```python
class Solution:
    def solve(self, ...): ...
```
```

adapter 解析规则（调用 `skills/parser.py::parse_problem_markdown`，确定性、非 NLP）：
- 按 `## ` H2 切分各字段；代码块用 ```` ```python ```` 围栏提取（参考旧 `_strip_fences`）。
- `test_cases`：复用 `code_tutor_agent.leetcode.leetcode_fetcher._parse_examples_to_test_cases(examples_raw, "")`
  （与旧实现一致，从 `## Examples` 段解析）。
- `difficulty`：归一化为 `easy|medium|hard`（兼容大小写/中文，统一转小写或映射）。
- 解析失败（缺必需段 / 关键字段空 / 代码无法 compile）→ 抛 `SkillExecutionError`，**不静默返回空**。
- 归一后字段对齐 `ProblemMeta`：`title / topic / difficulty / description / starter_code / optimal_solution / test_cases`。

**`cta-generate-solution`**：入参为整题 `description`（经
`arguments={"$ARGUMENTS": description}` 注入）；输出为自由 markdown，**不做结构化解析**，
`generate_detailed_solution` 直接返回 `output` 文本（与旧 `generate_detailed_solution_via_skill_sync` 行为一致）。

### 6.5 LLM 注入与单一真源
- adapter 通道统一 `get_llm(get_skill_engine_llm_alias())`，确保 import / CLI / CI 三通道解析到
  同一 model/base_url/key，杜绝"CLI 读另一套 env"的分裂。
- CLI 逃生舱若带 `--llm`，沿用 skill-engine 自身 LLM 配置（AGNES_*），作为调试/兜底通道可接受。

### 6.6 错误处理
- 捕获 `skill_engine` 异常 → 转 `SkillConfigError`（用户参数错 / skill 未找到 / LLM 不可用，可重试提示）
  或 `SkillExecutionError`（引擎内部 / 解析失败，告警 + 5xx 语义）。
- `run_skill` 对外**永远返回 `SkillResult`**，不向外抛未处理异常（便于调用方按结果处理）。

---

## 7. 保留的 CLI 逃生舱（降级为 opt-in）

> 文件：`src/code_tutor_agent/agents/skill_cli.py`（保留，定位收窄）

### 7.1 定位变化
- 旧文档把它当"备选/扩展通道"；本设计把它当**显式 opt-in 的逃生舱**：
  仅当 `mode="cli"` / skill 在实验白名单 / 用户聊天显式喊"用 CLI 跑" / 复现 CI 时才走。
- 保留其全部护栏（见下），因为它仍是常驻服务里唯一 spawn 子进程的地方，安全要求不变。

### 7.2 护栏（必须保留，不可省略）
1. **skill 白名单**：仅允许预置列表 + 实验白名单，拒绝即返回 `ok=False`，**绝不拼进 shell**。
2. **超时进程树强杀**（Windows 重点）：`CREATE_NEW_PROCESS_GROUP` + `taskkill /T /F`（或 POSIX `killpg`）。
3. **输出截断**：stdout/stderr 各限长（如 50KB），防上下文撑爆。
4. **键值传参**：一律 list 式 argv（`-a "k=v ..."`），禁止字符串拼接。
5. **cwd 指对**：`run` 扫 `<cwd>/skills`，spawn 时 `cwd=get_skill_engine_dir()`（或复制受控临时目录）。
6. **编码防御**：`encoding="utf-8", errors="replace"` + `out/err or ""`，防 Windows GBK 崩。

### 7.3 与 adapter 的接口对齐
- `skill_cli.run_skill_cli` 返回结构升级为与 import 主通道共用的 `skills.result.SkillResult`
  **同形**（Phase 4 已抽出 `skills/result.py`），使上层
  （`tools.py` 路由 / `generator_node` 兜底 / Admin）不感知通道差异，统一以 `.ok` / `.output` / `.error` 消费。
- CLI 通道的解析**复用 `skills/parser.py` 共享模块**（见 DP-5），不再在 `skill_cli` 内各自维护
  一份 parser；双通道结构一致由共享代码从源头保证，§12 契约测试退化为验证 parser 行为。

---

## 8. 配置单一真源

在 `config.py` 增加（保留现有 `get_skill_engine_dir` / `get_skill_engine_cli_timeout` /
`SKILL_ENGINE_CLI_ALLOWLIST`，并新增以下）：

```python
from pathlib import Path as _Path

def get_skill_engine_llm_alias() -> str:
    """adapter 通道使用的 LLM 别名（单一真源），默认 'agnes'。"""
    return os.getenv("SKILL_ENGINE_LLM_ALIAS", "agnes")

def get_skill_engine_skills_root() -> str:
    """本系统内置 skill 目录（随仓发布）；adapter 与 cli_runner 共用。
    注意：当前分支的 skill 实际存放位置需核对（见 §9 备注），
    以下默认指向 skills/defs，落地时按真实路径校正。"""
    return os.getenv(
        "SKILL_ENGINE_SKILLS_ROOT",
        str(_Path(__file__).parent / "skills" / "defs"),
    )
```

> 三通道（adapter import / cli_runner `--llm` / CI `--llm`）都解析到同一 LLM 别名。

---

## 9. 接入点改造

### 9.1 `agents/tools.py`：工具路由（默认 adapter，opt-in CLI）
- 新增模块级开关/参数 `SKILL_CHANNEL_DEFAULT = "adapter"`。
- `generate_problem_via_skill(topic, difficulty, *, mode="adapter")`：
  - `mode="adapter"` → `await asyncio.to_thread(engine_adapter.generate_problem, ...)`
    （内部已是同步逻辑，to_thread 防阻塞事件循环）。
  - `mode="cli"` → `await asyncio.to_thread(skill_cli.run_skill_cli, ...)` 后解析（沿用旧 `_sync` 核心）。
- `generate_detailed_solution_via_skill(description, *, mode="adapter")` 同理。
- 工具 `description` 明确告知 LLM：默认走 import 通道；仅当用户显式要求 CLI / 调试 skill 时传 `mode="cli"`。
- 函数仍定义在 `tools.py`（满足 `run_tool_loop` 的 `getattr(_self_module, name)` 解析约束），
  sync 核心来自 `engine_adapter` / `skill_cli`。

> **备注（DP-4 已拍板）**：白名单精简为两个真实 def——
> `cta-generate-problem` + `cta-generate-solution`（即详细题解 skill）；**移除幽灵名 `cta-generate-solution`**（当前 `skill_cli.py` 引用了它，但无对应 def，落地时一并修正为 `cta-generate-solution`）。
> 落地前仍须与真实 defs 名核对（专属 skill 按 **DP-3 放本仓 `src/code_tutor_agent/skills/defs/`**，
> 不再是"skill-engine 仓内或仓库某处"的不确定状态）。

### 9.2 `nodes/generator.py` 路径 C：改走 adapter
将当前 `generator.py:373-391` 的 `run_skill_cli` + `parse_problem_markdown` 兜底，
替换为 `engine_adapter.generate_problem(...)`，拿回 `ProblemMeta` 后**复用现有落库 / SSE 链路**（不变）：

```python
else:
    # ── 路径 C：进程内 import 通道（主通道失败后才走，进程内、结构化）──
    logger.warning("LLM 出题 %d 次失败 — 尝试 skill-engine (import)", MAX_ATTEMPTS)
    _progress(sid, "⚠️ 进程内出题失败，尝试 skill-engine 出题…")
    writer("⚠️ 进程内出题失败，尝试 skill-engine 出题…")
    try:
        from code_tutor_agent.skills.engine_adapter import generate_problem as _adapter_gen
        # 校正（2026-07-18 实落反馈）：
        #  1) generator_node 是**同步 def**（非 async），故直接同步调用，
        #     不加 await / asyncio.to_thread；
        #  2) generate_problem 返回**扁平 problem dict**（非 ProblemMeta，
        #     无 model_dump），save_problem 直接吃扁平 dict，与旧 CLI 解析同形。
        problem_dict = _adapter_gen(topic, difficulty, max_retries=1)
        sample_tcs = problem_dict.get("test_cases", [])[:2]
    except Exception as exc:   # 任何失败都降级，不冒泡
        logger.warning("skill-engine 出题也失败 — 回退静态题库: %s", exc)
        _progress(sid, "⚠️ skill-engine 失败，切换到静态题库…")
        writer("⚠️ skill-engine 失败，切换到静态题库…")
        problem_dict = get_static_problem(topic=topic, difficulty=difficulty) or get_static_problem()
        sample_tcs = [tc for tc in problem_dict.get("test_cases", []) if not tc.get("is_hidden", False)][:2]
```

> 关键：失败兜底语义不变（路径 D 静态池继续兜底），仅把"CLI 逃生舱"换成更稳的"import 通道"。
> 仍可用 `mode="cli"` 的旧链路作为更深的逃生舱（极少触发）。

### 9.3 辅导环节详细题解（`TUTOR_TOOLS`）
`generate_detailed_solution_via_skill` 默认走 `engine_adapter.generate_detailed_solution`，
`mode="cli"` 时回退到 `skill_cli.run_skill_cli("cta-generate-solution", ...)`。
`chat.py` 现有绑定无需大改，仅切换底层通道。

---

## 10. 可观测性（轻量，比例恰当）

- 在 `engine_adapter` 与 `skill_cli` 边界打结构化日志：skill 名、入参 keys（脱敏不记值）、
  通道（adapter/cli）、耗时、ok/error、产物数量。
- 轻量进程内指标（参考 `feat/cli` 的 `skills/metrics.py`，可选）：
  `record_skill_run(skill, channel, status, duration_s)`，`status ∈ {ok, error, rejected, timeout}`。
- 逃生舱的超时 / 白名单拒绝必须 `logger.warning` 告警。
- 不引入 Prometheus 重依赖（与系统定位一致）。

---

## 11. 测试策略

### 11.1 单元测试（mock 适配层边界）
- adapter：`patch` `skill_engine` 的 `Runner.run`，断言 `_bootstrap` 正确翻译入参、正确归一化、
  错误被正确封装为 `SkillConfigError` / `SkillExecutionError`。
- CLI：沿用现有 `test_skill_cli.py`（mock `subprocess.run`），断言护栏（超时强杀 / 截断 / 白名单拒绝 / 键值传参不拼 shell）**保持全绿**。
- `test_agent_tools.py`：新增 `mode` 参数断言——默认走 adapter、显式 `mode="cli"` 走 subprocess。

### 11.2 契约测试（golden test，兼容核心）
- 固定 skill + 固定参数，跑一次取得"基准输出快照"，断言输出结构稳定（字段存在、类型正确）。
- 升级 `skill-engine` tag 后先跑此测试，第一时间发现破坏性变更。位置：`tests/test_skill_engine_contract.py`。
- 因 **DP-5 双通道共用 `skills/parser.py`**，此测试退化为**验证共享 parser 行为**（同一份代码，
  import/CLI 自然一致），无需再断言两通道解析结果"交叉相等"。

### 11.3 集成测试 + CI 回归
- 集成：`engine_adapter.generate_problem` 真实跑一次（需 LLM key），断言返回合法 `ProblemMeta`。
- CI 回归：`scripts/skill_smoke.py`（subprocess 同形，缺 LLM key 自动 SKIP），失败阻断合并。
- 建议在 CI 也加一条 import 通道冒烟（若环境有 key）。

---

## 12. 实施路线图（分阶段，每阶段可独立验证）

- **Phase 0 — 依赖接入与 API 核对（✅ 已完成）**
  - ✅ 确认 `skill-engine` 已 editable 装进 code-tutor-agent venv（`uv run python -c "import skill_engine"` 通过，指向兄弟仓 `D:/Code/PycharmProjects/skill-engine`）。
  - ✅ 已核对当前版本真实 API：import 路径为 `skill_engine.routing.*` 与 `skill_engine.execution.*`（见 §6.3 核对记录）；`MatchResult` 签名与 `Runner.run(match_result, llm=)` 已确认；完整 in-process 管线用假 LLM 跑通验证。
  - ✅ 建 `src/code_tutor_agent/skills/__init__.py`；**专属 skill 按 DP-3 放 `src/code_tutor_agent/skills/defs/`**，
    已从 `feat/cli` 还原两个真实 def：`cta-generate-problem`、`cta-generate-solution`（即详细题解 skill）。
    **修正 DP-4 幽灵名**：当前 `skill_cli.py` 引用的 `cta-generate-detailed-solution` 无对应 def，统一为真实名 `cta-generate-solution`（落地 Phase 4 时一并改 `skill_cli.py`）。
- **Phase 1 — 适配层 + 共享解析**
  - 实现 `skills/result.py`（`SkillResult` 双通道共用结构）+ `skills/engine_adapter.py`（`_bootstrap` / `run_skill` / `generate_problem` / `generate_detailed_solution` / 错误类型；`SkillResult` 从 `result.py` re-export 保持兼容）。
  - **抽 `skills/parser.py` 共享解析模块**：从 `skill_cli.parse_problem_markdown` 迁出
    （`parse_problem_markdown` / `_strip_fences` / `_parse_examples_to_test_cases`），adapter 与 cli 共用（DP-5）。
  - `config.py` 新增 `get_skill_engine_llm_alias` / `get_skill_engine_skills_root`；**白名单按 DP-4 精简为两个**。
  - adapter 单测（mock `Runner.run`）+ 契约测试初版（验证 `parser.py` 行为）。
- **Phase 2 — 工具路由**
  - `tools.py`：`generate_problem_via_skill` / `generate_detailed_solution_via_skill` 增加 `mode` 参数，默认 `adapter`。
  - 现有 `SKILL_TOOLS` / `TUTOR_TOOLS` 注册不变，仅底层通道切换。
  - `test_agent_tools.py` 扩展 `mode` 分支。
- **Phase 3 — 出题热路径接线**
  - `generator.py` 路径 C 改走 `engine_adapter.generate_problem`，复用落库/SSE；保留静态池兜底。
  - 集成测试：import 通道出题返回合法 `ProblemMeta`，端到端不崩。
- **Phase 4 — CLI 逃生舱收窄**
  - `skill_cli.run_skill_cli` 返回结构与 `SkillResult` 同形；保留全部护栏。
  - CLI 通道**复用 `skills/parser.py` 共享模块**（不再在 `skill_cli` 内各自维护 parser，DP-5）。
  - 确认 `test_skill_cli.py` 全绿（未回归）。
- **Phase 5 — 可观测 + CI**
  - 轻量 `skills/metrics.py` + 边界日志/告警。
  - `scripts/skill_smoke.py` 真实 CLI 回归（缺 key 自动 SKIP）。
- **Phase 6 — 文档收口**
  - 本设计文档取代旧 `skill-engine-cli-escape-hatch-design.md` 的架构定位；旧文档可标注"历史/被取代"。

---

## 13. 风险与对策

| 风险 | 对策 |
|---|---|
| `skill_engine` 公开 API 与 §6.3 不符 | Phase 0 先核对真实导出；适配层收敛所有 `import`，不符处只在适配层改 |
| 专属 skill 实际路径与默认 `skills/defs` 不符 | Phase 0 核对真实路径，`get_skill_engine_skills_root` 校正 + `discover(roots=[绝对路径])` 不经 cwd |
| 出题契约缺 `OptimalSolution` 导致校验网关失败 | 解析时强制要求 `optimal_solution` 非空，否则 `SkillExecutionError` 触发重跑 |
| 进程内长调用阻塞事件循环 | 所有 adapter 调用经 `asyncio.to_thread` 包一层（沿用 `tools.py` 现有模式） |
| 逃生舱被提示注入滥用 | 保留白名单 + 超时进程树强杀 + 输出截断 + 键值传参；工具描述约束默认走 import |
| 两条通道输出结构漂移 | **DP-5 已根除**：adapter 与 cli 共用 `skills/parser.py`，单一解析源码，无双份实现 |
| LLM 配置三方不一致 | §8 单一真源别名，三通道共用 |
| 热路径引入后出题行为变化 | 默认维持原 LLM 主通道（路径 B）；import 通道仅作路径 C 兜底，灰度可控、可回滚 |

---

## 14. 演进空间（非本次实现，仅记录）

- 若未来需被多个 agent/服务共享，可将 `skill-engine` 包成 MCP server，本系统改为 MCP client；
  适配层可平滑改为 MCP 调用，业务调用方不变。
- 若 skill 数量增多，可在 Admin 增加 `skill list/info` 透传端点。
- 若担心进程内 skill 调用拖垮主进程，可保留"逃生舱 subprocess"作为隔离边界（本设计已保留）。

---

## 15. 验收标准

- [x] `skill-engine` editable 装进 code-tutor-agent venv，`import skill_engine` 与 `skill-engine` 命令均可用。
- [ ] 全系统仅 `engine_adapter.py` 直接 `import skill_engine`；`skill_cli` 不 import。
- [ ] `engine_adapter.generate_problem` 返回合法 `ProblemMeta`，其出题契约含 `## OptimalSolution`，解析与旧 `parse_problem_markdown` 行为一致。
- [ ] `generate_detailed_solution(description)` 返回 markdown 文本，与旧 `generate_detailed_solution_via_skill_sync` 行为一致。
- [ ] `tools.py` 工具默认走 adapter（`mode="adapter"`），显式 `mode="cli"` 才走 subprocess。
- [ ] `generator_node` 路径 C 改走 `engine_adapter.generate_problem`，静态池兜底不变，端到端不抛异常。
- [ ] `skill_cli` 对非法 skill 名拒绝、超时进程树强杀、超长输出截断均生效（现有测试全绿）。
- [ ] 契约测试在升级 `skill-engine` 时能有效拦截破坏性变更，且断言 import / CLI 双通道解析一致。
- [ ] 轻量指标 + 边界日志/告警落地。
- [ ] `scripts/skill_smoke.py` 真实 CLI 回归可用，缺 LLM key 自动 SKIP。

---

## 16. 与既有文档的关系

- **取代** `docs/skill-engine-cli-escape-hatch-design.md` 的架构定位判断：
  旧文档把 subprocess CLI 当"备选/扩展通道"；本文档将其**降级为显式 opt-in 逃生舱**，
  并把**进程内 import 抬为主通道**。旧文档的 §2（工具底座约束）、§7（失败降级语义）、
  §10（测试策略的 mock 套路）仍有效，可继续参考。
- **复用** `docs/skill-engine-integration-design.md`（`feat/cli` 分支）的结构与实现细节：
  §6 适配层、`skill_engine` 公开 API 序列、§7 CLI 护栏、§10 轻量指标、§11 测试分层。
  本文档的 `engine_adapter` 接口与解析规则向该文档对齐，但**契约字段以当前分支真实 parser 为准**
  （`parse_problem_markdown`：title/topic/difficulty/description/starter_code/optimal_solution/test_cases）。
