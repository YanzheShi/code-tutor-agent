# CodeTutor Agent

> AI 编程导师，不是 OJ。
> 具备"自主出题 → 执行判题 → 渐进辅导 → 轨迹分析 → 长期画像"完整认知闭环的刷题系统。

![Python](https://img.shields.io/badge/Python-3.12+-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-1.x-orange)
![LangChain](https://img.shields.io/badge/LangChain-1.3-green)
![React](https://img.shields.io/badge/React-19-61dafb)
![License](https://img.shields.io/badge/license-MIT-blue)

---

## 为什么不是又一个 LeetCode Clone

传统 OJ 的逻辑是线性的：`用户提交 → 跑用例 → 对答案 → 结束`。
CodeTutor Agent 的逻辑是 ：能够根据用户个人的情况出题，知道能够帮助用户分析在写解题过程中出现的卡壳问题、分析思考过程，给出专家级的指导和提升意见。并记录用户画像。做到一个真正的代码编程导师。

几个真正区别于普通 OJ 的差异点：

- **出题器自验证闭环**：题是自己出的，参考解是自己写的，测试用例是自己跑的——LLM 产出后先过结构 / 编译 / 思维链泄露三重校验，再用参考解在沙箱里把示例跑一遍回填期望输出；任一环不过就**整题作废重采样**，落库后后台异步生成随机 + 边界用例并对拍，跑挂的用例直接丢弃。跑不出可靠用例的题不交付。**盲信 LLM 的反面。**
- **判题节点执行 + 温暖反馈**：用户代码提交后由 Judge0 沙箱跑全量测试用例，再由 LLM 解读结果，给出**温暖、面试导向**的反馈与修复建议；AC 时还会做时空复杂度分析与优化方向提示。verdict 永远以执行引擎客观结果为准，LLM 只负责文案，不臆造失败输出。
- **🔥 导师对话 Agent 工具循环 + 误解诊断**（唯一真正带工具循环的组件）：不是 "WA at #3"，而是结合**用户编辑器实时草稿**与运行/判题客观结果，定位卡点并给针对性建议。
- **规划节点（选题器）长期画像驱动选题**：跨会话维护 **per-tag 知识点画像**（熟练度 ELO / 稳定性 / 遗忘）+ **6 维错误模式画像**，下一题不是随机出，而是选"最该练"的薄弱点。
- **护栏节点（Critic）负责换题路由与 episode 收尾**：在一题终结（真实提交 AC / 换题 abandon）时 flush `problem_history`、做挫败情绪检测并路由到下一题。⚠️ 这是**纯旁路的收尾节点，不做内容守门**——代码泄露过滤（R01）当前是未生效的死逻辑，挫败情绪检测（R04）只打日志无后续动作，详见[已知限制](#已知限制)。

---

## 功能演示

> 以下截图来自系统真实运行界面，存放于 [`demo/`](demo/) 目录。

### 出题 & 选题

| 出题主页面 | 选题 |
| --- | --- |
| ![出题主页面](demo/出题主页面.png) | ![选题](demo/选题.png) |

- **出题主页面**：AI 自主出题 + 自验证闭环（双解 + 示例 → 结构 / 编译 / 无思维链泄露三重校验 → 参考解沙箱自跑回填示例期望输出 → 任一环不过则整题重采样），并展示题面、参考解与测试用例。完整测试用例在题目落库后由后台异步生成并对拍，不挡首屏。出题内置防碰撞机制：每次随机二选一注入「场景灵感（F）」或「算法维度（G）」，避免重复产出经典原题。
- **选题**：规划节点（选题器）依据用户画像（最弱知识点 / 错误模式）智能选题，而非随机出题。

### 出题子流程

![出题子流程](demo/出题子流程.png)

> 出题器（Generator）的决策树：LeetCode 链接导入（贴了 URL 走这条，失败直接报错、**不替用户换题**）→ 原创 LLM 生成（双解 + 示例）→ verify 结构 / 编译 / 无思维链泄露 → 参考解沙箱自跑回填示例期望输出，任一环不过则重采样重试；全部失败后按 **LeetCode 按主题拉题 → 历史未 AC 题 → 静态题库**三级降级。

### 做题 & 导师辅导

| 做题主页面 | 导师引导 |
| --- | --- |
| ![做题主页面](demo/做题主页面.png) | ![导师引导](demo/导师引导.png) |

- **做题主页面**：内置 Monaco 在线代码编辑器，提交后走「Judge0 判题 → LLM 温暖反馈 → 下一轮」闭环。
- **导师引导**：基于**题面 + 编辑器实时草稿 + 客观运行/判题结果**定位问题并给建议，可调用 `judge_run_code` 现场验证用户代码。
- **联网搜索（可选）**：导师配有 `web_search` 工具，被要求「联网搜/查最新版本」或涉及时效性事实时会先搜再答；对接自建搜索 MCP（详见下方「联网搜索」小节），未配置时该工具自动不暴露。

### 🔥 链路追踪辅导（核心亮点）

| 轨迹分析 | 导师与轨迹分析同屏 |
| --- | --- |
| ![轨迹分析](demo/轨迹分析.png) | ![导师于轨迹分析同屏](demo/导师于轨迹分析同屏.png) |

- **轨迹分析**：全流程采集用户的**编辑轨迹（edit-trace）**——每一次键入(edit)/停顿(idle)/运行(run)/提交(submit) 四类结构化事件都入轨，由轨迹分析模块做分析（edit-trace analysis / summarize），反推真实思考路径与卡点，把"为什么卡住"变成可观测数据。
- **导师与轨迹分析同屏**：导师辅导界面与轨迹分析**并排展示**，轨迹证据用于反推卡点、区分"不会用哈希表"还是"知道但写不对 dict"。
  **轨迹摘要为双落点**（`trace/summarize.py`，过渡时由前端 `POST /analyze/summarize` 触发落库）：
  ① **可见卡**——主聊天渲染 `summary_text` + `bullets`（`useSession.ts` 的 `trace-summary` 气泡）；
  ② **跨题上下文注入**——`/next-problem` 时 `session.py` 读 `get_trace_summary()` 追加「## 上一题轨迹分析摘要（仅供导师参考，勿直接复述给用户）」段落进 `context_summary`，经 `build_transcript_with_budget()` 前置为「## 对话摘要」，最终进入下一题**选题对话**的 prompt（`analyze_user_intent`）。

### 用户画像（六维错误模式 + 知识点画像）& 成本中心

| 我的画像 | 用户画像 | 用户画像 2 | 成本中心 |
| --- | --- | --- | --- |
| ![我的画像](demo/我的画像.png) | ![用户画像](demo/用户画像.png) | ![用户画像2](demo/用户画像2.png) | ![成本中心](demo/成本中心.png) |

- **六维错误模式画像**：跨知识点的通病画像，覆盖 **正确性 & 边界 / 数据结构操作 / 复杂度 & 性能 / 算法思维 / 实现质量与鲁棒性 / 自测与调试** 共 6 个维度（`correctness / datastruct / perf / algo / impl / debug`）。由判题失败与轨迹分析双路 feeder 写入，时间衰减 + 叠加聚合，前端以纯 SVG **六维雷达图**呈现。
- **知识点画像（per-tag）**：32 个算法/数据结构 Tag（定义在 `profile/tags.py`：array 6 + 链表 3 + 栈/队列/堆 4 + 树/图 7 + DP 4 + 字符串 3 + 其他 5），每个 Tag 维护熟练度 ELO / 稳定性 / 遗忘衰减 / 错误指纹 / 提交记录，规划节点（选题器）据此选题。
- **成本中心**：Token 用量与预算看板（调用 `/admin/token` 接口），可逐目的、逐模型、逐会话下钻。

---

## 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.12+ | LG 1.x / LC 1.x 锁 3.11+ |
| 包管理 | uv | 快，pyproject.toml 驱动 |
| 流程编排 | LangGraph 1.x StateGraph（8 个编排节点）+ checkpointer + store | 状态机 / 会话状态持久化 / human-in-the-loop (`interrupt`) |
| LLM 调用 | langchain-openai + base_url  | OpenAI 兼容口，DeepSeek / 通义 / 自建都能走 |
| API 层 | FastAPI + uvicorn | /session /submit /chat /admin |
| 前端 | React 19 + Vite 6 + TypeScript | SPA，Monaco 编辑器，Tailwind 样式，纯 SVG 雷达图 |
| 判题沙箱 | Judge0（Docker）/ 本地 subprocess 兜底 | 资源隔离 + 用例执行 |
| 状态持久化 | langgraph-checkpoint-sqlite（会话级 checkpointer） | 单机会话恢复 |
| 长期记忆 | LG `InMemoryStore`（当前）→ 规划迁移 RedisStore | 画像跨会话；⚠️ 当前为进程内存，**重启即丢**，见[已知限制](#已知限制) |
| 可观测性 | LangSmith | 多节点链路追踪，便于调试排障 |
| 数据建模 | Pydantic v2 | LC/LG 原生 |

---

## 架构：出题 / 判题 / 辅导 / 规划 / 评审 + 轨迹分析

```mermaid
flowchart TB
    PG[出题器<br/>Generator<br/>自验证闭环]
    PJ[判题节点<br/>Judge<br/>Judge0 执行 + LLM 反馈]
    PT[导师对话 Agent<br/>Tutor<br/>误解诊断 + 渐进提示<br/>⚠ 等级阶梯未接通]
    PP[规划节点<br/>Planner<br/>画像驱动选题]
    PC[护栏节点<br/>Critic<br/>episode 收尾 + 换题路由<br/>⚠ 代码泄露过滤不生效]
    CHAT[辅导聊天通路<br/>api/routers/chat.py<br/>LLM 流式直出<br/>⚠ 不经 StateGraph 无后置校验]

    TR[轨迹分析<br/>edit-trace → 模块分析<br/>6 维错误模式]
    UPS[共享状态中心<br/>用户画像服务<br/>per-tag 知识点 + 6 维错误模式<br/>checkpointer + store]

    PG --> PJ
    PJ --> PT
    PT --> PP
    CHAT --> PT
    CHAT -.->|回复直接落库 不过 Critic| UPS
    PJ -.->|仅 AC 后经 update_profile| PC
    PP --> PG
    PC -.->|换题时路由下一题| PP
    PT --> TR
    PJ --> TR
    TR --> UPS
    PG <--> UPS
    PJ <--> UPS
    PT <--> UPS
    PP <--> UPS
```

> **术语说明**：本系统是一个 LangGraph **状态机**，上图方框均为单一职责的**编排节点**。其中仅「导师对话 Agent」真正使用工具循环（`judge_run_code` / `web_search`）；出题器是带多通道降级的出题流水线；判题节点是「沙箱执行 + LLM 反馈文案」；规划节点是**规则引擎（非 LLM）**；
>
> ⚠️ **上图是概念角色图，不是代码拓扑**。StateGraph 实际注册 **8 个节点**（`graph/graph.py`），比上图多出的三个是：
> - `wait_for_submit_node` —— 全图**唯一**调用 `interrupt()` 的地方，做题期间图在此挂起等用户提交；
> - `update_profile_node` —— v2 画像的单 writer（定义在 `profile/node.py`，不在 `nodes/` 目录下）；
> - `agent_tutor_node` —— 判题非 AC 后的辅导路由节点（真正的导师回复**不走**它，见下条调用契约）。

**调用契约（产品态）**：

- 规划 → 可下调出题（下发选题偏好）
- 判题 ↛ 辅导（只能**交棒**，由 LG 图的 conditional edge 走，不是函数调用）
- **辅导回复 ↛ 评审**：做题阶段的导师回复由 `api/routers/chat.py` 的 `_handle_normal_chat_stream` 直接调 LLM 并流式吐给前端，随后 `pause_safe_update` 落库——**全程不进 StateGraph**，因此图上不存在任何能看到这条回复的节点。
- 评审 **无任何主动调用**，仅在一题终结时被触发（flush 历史 + 情绪检测 + 路由）
- **画像写入是三轨，不是单一入口**，务必分清：
  - **v2 per-tag 知识点画像**：判题 / 辅导想改画像只能挂 `state["profile_delta"]`，由 `update_profile_node`（`profile/node.py`）作为**唯一 writer** 写 `store`；
  - **v1 DBProfile**（旧版整体熟练度）：判题节点直接调 `db.update_profile_on_result()` 写 SQLite；
  - **6 维错误模式画像**：由 `fire_and_forget_error_mode_analysis()` 后台线程异步写，不进主链路。

> 详细设计文档见本地 `docs/` 目录（设计评审用，未纳入 git 追踪）。

---

## 开发进度

| 阶段 | 范围 | 状态 |
|---|---|---|
| 核心闭环 | 出题自验证 + 判题（Judge0 执行 + LLM 温暖反馈）+ 辅导 + 评审收尾路由 | ✅ 已实现 |
| 🔥 链路追踪辅导 | edit-trace 采集 + 轨迹分析 / 复盘 + 导师同屏联动 | ✅ 已实现 |
| 导师输出防泄露闸门 | prompt 硬约束 + post-check 双层守门（拦截"直接给我答案"类诉求） | ⚠️ **未生效**，见[已知限制](#已知限制) |
| 用户画像 | per-tag 知识点画像（ELO / 稳定 / 遗忘）+ 6 维错误模式画像 + 智能选题 | ✅ 已实现 |
| 出题防碰撞 | 随机二选一注入场景（F）/ 维度（G），维度数据覆盖 20 个知识点 | ✅ 已实现 |
| 成本中心 | Token 用量 / 预算看板（按目的 / 模型 / 会话下钻） | ✅ 已实现 |
| Docker 部署 | Judge0 沙箱（按需启用）+ nginx 反向代理 | ✅ 已实现 |
| V0.5 | Debug 剧场 / 面试模拟 / 同伴对比 / 跨语言迁移 | ⏳ 规划中 |
| V1.0 | 全 PRD 功能 | ⏳ 规划中 |

---

## 已知限制

- **用户画像（v2）尚未持久化**：存画像的 `store` 当前是 `InMemoryStore`，**进程重启即丢**。`graph/graph.py::compile_graph()` 只给 `checkpointer` 配了 `SqliteSaver`（会话状态是落盘的）。换 `RedisStore` / SqliteStore 对业务代码零改动（LG 的 `store.list/put/get` 接口统一），但尚未实施。

- **导师输出无后置守门（Critic 的 R01/R04 是死逻辑）**：`nodes/critic.py` 里的代码泄露过滤（R01）**从未生效过**——它原地改写 pydantic state 不被 LangGraph 采纳、只检查最后一条消息、且 `hint_level` 全仓库从未递增恒为 0；挫败情绪检测（R04）只 `logger.info`，无后续动作。历史上的 `constitutional_guard` 节点已随 normal 模式一并删除。
  - **根因**：做题阶段的导师回复由 `api/routers/chat.py::_handle_normal_chat_stream` 直接调 LLM 并流式吐给前端，**全程不进 StateGraph**——因此图内根本不存在任何能看到这条回复的节点，在图里改连线永远修不好。
  - **正确修法**：闸门必须加在 `chat.py` 的回复通路上（yield 之前），而不是图内节点。

- **暂不支持设计类题目**：判题引擎（本地沙箱 / Judge0）目前按 LeetCode「单方法 `class Solution`」契约执行用例——即实例化 `Solution` 后单次调用其方法比对结果。需要实现**带多个方法的类并按操作序列多次调用**的设计类题目（如 LRU 缓存、最小栈、实现前缀树）无法被正确执行。因此出题侧已主动拦截此类题目：
  - LLM 原创出题：prompt 禁止生成 + 校验器拦截（`generation/verifier.py`、`agents/agent_problem.py`）；
  - LeetCode 链接导入 / 按主题拉题：识别到顶层非 `Solution` 类（如 `class LRUCache`）时拒绝导入并给出明确提示。

  支持设计类题目（操作序列格式的测试用例 + 对应判题分支）已在后续规划中。

---

## 项目结构

```
code-tutor-agent/
├── pyproject.toml
├── Makefile
├── .env.example
├── data/                   # 运行时数据（SQLite / checkpoints）
├── docker/                 # Docker / docker-compose 配置
│   ├── Dockerfile
│   ├── Dockerfile.frontend
│   ├── docker-compose.yml
│   ├── docker-compose.prod.yml
│   └── nginx-frontend.conf
├── scripts/               # 启动脚本（start-all.bat/.sh 等）与工具脚本
├── demo/                  # 系统功能截图（README 展示用）
├── src/
│   └── code_tutor_agent/
│       ├── api/           # FastAPI 入口（main.py + routers/）
│       │   ├── main.py        # app 装配 + /health
│       │   └── routers/       # session / chat / run / problems / admin / token
│       ├── db/            # 数据库模块（用户画像 / 轨迹 / 题目落库）
│       ├── graph/         # LangGraph StateGraph 定义
│       ├── nodes/         # LangGraph 节点函数（编排步骤，7 个）
│       │   ├── planner.py     # 规划（画像驱动选题，规则引擎非 LLM）
│       │   ├── generator.py   # 出题（调用 generation 出题子系统）
│       │   ├── agent_judge.py # 判题（Judge0 执行 + LLM 温暖反馈）
│       │   ├── agent_tutor.py # 辅导路由（非 AC 循环等待重提交）
│       │   ├── critic.py      # 评审（episode 收尾 flush + 换题路由；⚠ R01/R04 为死逻辑）
│       │   ├── agent_dialog.py # 导师对话 Agent（唯一带工具循环的组件）
│       │   └── wait_for_submit.py  # 全图唯一的 interrupt() 挂起点
│       ├── agents/        # LLM 调用封装（dialog 为唯一带工具循环的 Agent；judge / problem 为结构化输出封装）
│       ├── generation/    # 出题子流程（原创 LLM → 双解+示例 → verify → 跑参考解）
│       ├── profile/       # 用户画像（含第 8 个图节点 update_profile_node）
│       │   ├── node.py        # update_profile_node —— v2 画像单 writer
│       │   ├── schema.py      # 知识点画像（per-tag 5 字段）
│       │   ├── weakness.py    # 6 维错误模式画像（枚举 + 聚合）
│       │   ├── scoring.py     # ELO / 稳定 / 遗忘 打分纯函数
│       │   └── edit_trace_analyzer.py  # 轨迹 → 6 维错误模式增量
│       ├── trace/         # 编辑轨迹采集 / 预处理 / 轨迹分析 / 复盘
│       ├── token_usage/   # Token 成本统计 / 预算 / 看板
│       ├── sandbox/       # 代码沙箱（runner / judge0_client / 结构转换）
│       ├── leetcode/      # LeetCode 同步 / 题源
│       ├── mcp/           # MCP 工具与服务（judge0 server / 自建搜索 MCP 客户端）
│       ├── store/         # 画像 / session 状态存取
│       ├── schemas/       # Pydantic State + Request/Response
│       ├── prompts/       # Prompt 模板
│       ├── models/        # 数据模型
│       ├── tools/         # 工具函数
│       ├── memory.py      # 记忆层
│       ├── context_manager.py  # 上下文管理
│       ├── progress.py    # 进度流（SSE）
│       ├── topics.py      # 知识点 / 标签（32 个 Tag）
│       └── config.py
├── frontend/              # React 19 + Vite 6 + TypeScript SPA
│   └── src/components/    # MainLayout / CodeEditor / 导师对话面板 / AdminPanel(六维雷达) / CostCenter / 轨迹分析
├── tests/
└── docs/                  # 设计文档（本地评审用，未纳入 git 追踪）
```

---

## 快速开始

### 1. 安装

```bash
git clone https://github.com/YOUR_USERNAME/code-tutor-agent.git
cd code-tutor-agent
uv sync             # 安装后端 Python 依赖
cd frontend && npm install   # 安装前端 Node 依赖
cd ..
```

### 2. 环境变量

```bash
cp .env.example .env
# 编辑 .env 填入 API key
```

最小配置只需要一个 LLM 提供商（OpenAI 兼容 API）：

```ini
LLM_MODEL=gpt-4o
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=«your-api-key»
```

支持任何 OpenAI 兼容的 API 提供商（DeepSeek、通义千问、SenseNova、Ollama 等）。
完整配置项见 [.env.example](.env.example)。

### 3. 启动服务

**方式 A：一键启动（前后端同时）**

```bash
# Windows (双击)
scripts\start-all.bat

# 或 git-bash
bash scripts/start-all.sh

# 或 Makefile (git-bash)
make all
```

**方式 B：分别启动（开发调试）**

```bash
# 终端 1 — 后端 (port 8765, hot-reload)
make server
# 或 uv run uvicorn src.code_tutor_agent.api.main:app --host 0.0.0.0 --port 8765 --reload

# 终端 2 — 前端 (port 5173, 自动代理 API)
make frontend
# 或 cd frontend && npm run dev
```

**方式 C：Docker**

```bash
# 开发模式 (hot-reload)
cp .env.example .env
docker compose -f docker/docker-compose.yml up -d

# 生产模式 (nginx 反向代理)
docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml up -d
```

### 4. API 端点

```
POST   /session                              → 新建 session，出题 + interrupt 返题面
POST   /session/{sid}/submit                 → 提交代码，走判题→辅导→下一轮
POST   /session/{sid}/next-problem           → 回导师对话；引导文案按上一题 verdict×提示深度建议换题方向（AC 且提示用得深→同类型巩固，WA 且提示深→换方向/降难度）
GET    /session/{sid}/state                  → 前端轮询渲染
POST   /session/{sid}/chat/stream            → 导师对话（流式）
POST   /session/{sid}/edit-trace             → 上报编辑轨迹（edit/idle/run/submit 四类事件）
POST   /session/{sid}/analyze                → 触发轨迹分析
GET    /session/{sid}/analysis               → 获取轨迹分析结果
POST   /session/{sid}/analyze/summarize     → 生成会话复盘摘要
POST   /session/{sid}/run                    → 仅运行代码不判题
GET    /problems                             → 题目列表
GET    /problems/topics                      → 知识点 / 标签
POST   /admin/login                          → 管理端登录
GET    /admin/profile                        → 用户画像（旧版）
GET    /admin/profile/v2                     → 用户画像（新版 per-tag）
POST   /admin/token/overview                 → 成本中心看板数据
GET    /health                               → 健康检查
```

---

## Docker 部署

### 快速开始（开发模式）

```bash
# 1. 复制环境变量模板，编辑只填大模型配置即可
cp .env.example .env

# 2. 一键启动（前后端 + Judge0 判题沙箱）
#    Judge0、Redis、CORS 等变量已在 .env.example 提供默认值，无需手动配置
docker compose -f docker/docker-compose.yml up -d --build

# 3. 验证
curl http://localhost:8765/health
# 前端访问 http://localhost:3000
```

### 生产模式（nginx 反向代理）

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml up -d --build
# 前端 http://localhost:3000（nginx 代理所有 API 路由到后端）
```

### 环境变量说明

| 变量 | 必需 | 默认值 | 说明 |
|---|---|---|---|
| `LLM_MODEL` | 是 | — | LLM 模型名（OpenAI 兼容） |
| `LLM_BASE_URL` | 是 | — | LLM API 基础 URL |
| `LLM_API_KEY` | 是 | — | LLM API 密钥 |
| `JUDGE0_URL` | 否 | `http://localhost:2358` | Judge0 沙箱地址（设为 `http://judge0:2358` 由 compose 自动注入） |
| `JUDGE_BACKEND` | 否 | `self` | 判题后端切换：`self`（本地 subprocess）/ `judge0` |
| `SEARCH_MCP_URL` | 否 | `http://127.0.0.1:8080/mcp` | 搜索 MCP 端点（Streamable HTTP）；配置后导师获得联网搜索工具 |
| `SEARCH_MCP_TOKEN` | 否 | — | 搜索 MCP 的 Bearer token；未设置则不暴露 `web_search` 工具 |
| `SEARCH_MCP_TOOL_NAME` | 否 | `web_search` | 要调用的搜索工具名（对接非默认命名的搜索 MCP 时改这里） |
| `SEARCH_MCP_TIMEOUT_SECONDS` | 否 | `20` | 搜索单次调用超时秒数 |
| `CORS_ORIGINS` | 否 | `http://localhost:3000,...` | 前端跨域来源 |
| `VITE_API_BASE` | 否 | `http://localhost:8765` | 前端 API 基础 URL（构建时注入；生产模式设为 `/` 走同源代理） |

### 容器结构

- **backend**：FastAPI + uvicorn，端口 8765，挂载 `src/`（开发模式热重载）和 `data/`（持久化）
- **frontend**：Nginx 静态服务，端口 3000，代理 `/session/`、`/problems`、`/admin/`、`/health` 到后端（SSE 路径关闭缓冲）
- **judge0**：判题沙箱，含 PostgreSQL + Redis，需要 `privileged` 模式

### 移除 skill-engine

`skill-engine`（本地 Python 包）已从项目依赖中移除。详细题解功能改由导师 LLM 直接生成简单题解，无需额外安装。如需恢复 skill-engine 集成，请参考独立仓库 `skill-engine` 手动安装。

### LangSmith Trace

跑起来后 LangSmith 控制台能看到每条 session 的图内链路：
`generator(重采样重试) → agent_judge(执行+反馈) → update_profile(画像) → critic(收尾/路由) → planner(下一题)`

> ⚠️ **做题阶段的辅导回复不在此链路里**：它由 `api/routers/chat.py` 直接调 LLM，只有 LLM 调用自身产生 trace，**看不到任何图节点**。历史上的 `tutor(提示等级决策)` 节点已随 normal 模式一并删除，故 trace 中不会出现它。

换题（`/next-problem`）时 critic 的 ABANDON 分支清题进入对话，引导文案复用 `problem_history[-1]` 的 `verdict`

---

## 核心设计决策

### 判题：执行与反馈分离，verdict 客观唯一

- 判题由 **Judge0 沙箱客观执行**全量用例，verdict（AC / WA / RE / TLE）由执行引擎结果归约得出，**绝不交给 LLM 主观判断**。
- LLM 只负责生成温暖、面试导向的反馈文案与修复建议，且被强制注入权威 verdict，禁止自行改判或臆造失败用例的"实际输出"。
- AC 时仍做时空复杂度分析与优化方向提示（如暴力解可改哈希表）。

### 🔥 链路追踪辅导：edit-trace 即"教学可观测性"

- **全量编辑轨迹入轨 + 独立分析**：每次键入(edit)/停顿(idle)/运行(run)/提交(submit) 四类结构化事件都入轨（`POST /session/{sid}/edit-trace`），由 `trace/` 模块做预处理 + 轨迹分析（`/analyze` → `/analysis` → `/analyze/summarize`）。
- **摘要双落点**：分析结论压缩为 `TraceSummary` 后，一路渲染成主聊天的可见卡，一路在**换题时**追加进 `context_summary`、注入下一题的选题对话 prompt（`session.py` 读 `get_trace_summary` → `build_transcript_with_budget` → `analyze_user_intent`）。另有一路喂给 6 维错误模式画像。
- 轨迹分析反推**真实思考路径与卡点**：是"不会用哈希表"还是"知道但写不对 dict"，从行为而非结果判定，避免误判。
- **同屏联动**：导师辅导界面与轨迹分析并排展示，轨迹证据用于判定卡点类型。
- 这是把"为什么教不会"从玄学变成**可观测、可复盘数据**的核心——也是区别于一切 OJ / 答题器的关键能力。

### 双画像体系：知识点画像 × 错误模式画像

- **知识点画像（per-tag）**：32 个算法/数据结构 Tag（见 `profile/tags.py`），每个 Tag 维护 `prof`（ELO 熟练度 1000~4000）、`stab`（稳定性滑动窗 + 方差）、`forget`（遗忘衰减）、`errors`（错误指纹）、`attempts`（提交记录）。规划节点（选题器）据此选"最弱 tag"出题。
- **6 维错误模式画像（weakness）**：跨知识点的通病，维度为 `correctness / datastruct / perf / algo / impl / debug`。由**判题失败提取** + **轨迹分析**双路 feeder 写入，采用时间衰减 + 叠加聚合（久不犯自然淡出），LLM 输出被约束到固化 slug 防幻觉。前端以纯 SVG **六维雷达图**展示。

### checkpointer vs store（LG 1.x 两件套别混）

- **`checkpointer`（`SqliteSaver`，落盘）**：per-session，thread_id 绑，LG 原生恢复 State。这是**会话恢复与 `interrupt()` 挂起的依赖**——做题期间用户可能离开几十分钟，图靠它挂起和恢复，不占常驻资源。
- **`store`（当前 `InMemoryStore`，不落盘）**：cross-session，存 v2 per-tag 用户画像。⚠️ **进程重启即丢**，画像目前不是持久化的。接口走 LG 统一的 `store.list/put/get`，后期换 `RedisStore` 对业务代码零改动。
- 两者都在 `graph/graph.py::compile_graph()` 里装配：传了 `conn_string` 就用 `SqliteSaver`，否则退化 `InMemorySaver`（开发便利，生产请传）。


### 联网搜索：自建搜索 MCP（可选，厂商无关）

导师辅导对话可挂一个联网搜索工具 `web_search`，用于查最新版本、时效性资讯或对不确定事实做外部检索。接入是**厂商无关**的：任何以 Streamable HTTP 暴露、带 Bearer 鉴权、并提供兼容搜索工具的 MCP 服务都能用，配置即插即用，无需改代码。

- **按需暴露**：仅当配置了 `SEARCH_MCP_TOKEN` 时才把 `web_search` 注册进导师工具集，否则导师工具集保持原样（`agents/tools.py`）。
- **传输与鉴权**：走官方 `mcp` SDK 的 Streamable HTTP 客户端（JSON-RPC 2.0 / SSE 帧），Bearer token 由自建 `httpx.AsyncClient` 注入；`GET /healthz` 探活不需 token（`mcp/search_client.py`）。
- **工具名可配**：默认调 `web_search`；对接其他命名的搜索 MCP（如官方 Tavily 的 `tavily-search`）改 `SEARCH_MCP_TOOL_NAME` 即可。
- **失败降级**：搜索不可用 / 配额耗尽 / 上游失败时返回带提示的 JSON，引导导师回退到自身知识作答，而非空回复或误报。
- **明确触发**：系统 prompt 注入搜索使用规则（`api/routers/chat.py` 的 `_SEARCH_HINT`），用户要求「联网搜/查最新版本」或涉及时效性事实时先搜再答，避免凭过时记忆作答。

> 说明：官方原版 Tavily MCP（stdio、`tavily-search`、key 放服务端）与本接入默认「HTTP + Bearer + `web_search`」契约不同，直接对接需套网关或改传输方式，详见 `.env.example` 注释。

---

## 轨迹数据定期清理（运维）

细粒度编辑轨迹（edit-trace）数据增长较快，需定期瘦身。清理能力已内置：

- 清理函数：`src/code_tutor_agent/db/database.py` 的 `purge_trace_data(days=30, dry_run=False)`，只删「轨迹派生数据」，绝不碰 `submissions` / `profiles` / `problems` 等核心业务表：
  - `edit_traces`：前端全量采集的细粒度编辑事件（数据量最大）
  - `trace_threads`：多轮分析线程 transcript
  - `trace_analysis`：首轮结构化结论缓存
  - `analysis_results`：按题分析结果
  - `trace_summaries`：过渡摘要
  - 删除依据：各表 `updated_at` / `created_at` < `now - days`（默认 30 天）。
- `dry_run=True` 时只 `SELECT COUNT(*)` 统计待删行数、不执行删除；（默认 `False`）删除与统计共用同一份表/列定义，避免两处漂移。

### 一键清理脚本

`scripts/cleanup_traces.py` 封装了上述函数，供运维 / 定时任务调用：

```bash
# 清理 30 天前的轨迹数据（默认）
uv run python scripts/cleanup_traces.py

# 只保留最近 7 天
uv run python scripts/cleanup_traces.py --days 7

# 预览：只统计待删行数，不删除
uv run python scripts/cleanup_traces.py --dry-run

# JSON 输出（便于日志采集 / 监控）
uv run python scripts/cleanup_traces.py --json
```

退出码：`0` 成功 / `1` 清理失败 / `2` 参数或环境错误（供任务调度判成败）。
运行结果会追加到 `logs/cleanup_traces.log`。

### 注册系统定时任务（可选，当前未启用）

> 本项目默认**不注册**任何自动执行。如需定期运行，二选一：

**方式 A — Windows 任务计划程序**（系统级，需当前用户授权）：

```powershell
schtasks /Create /TN "CodeTutor-CleanupTraces" /SC DAILY /ST 03:00 /F /TR "D:\Code\PycharmProjects\code-tutor-agent\scripts\run_cleanup_traces.bat"
```

`scripts/run_cleanup_traces.bat` 已封装 `cd` + 调 `.venv\Scripts\python.exe`，避免路径/环境变量陷阱。如需锁屏/未登录也执行，再加 `/RU Andre`（会提示输入密码）。

**方式 B — WorkBuddy 自动化 / cron**：用 WorkBuddy 自动化（recurring，每天）或系统 cron 跑上面的 Python 命令即可。

管理命令：`schtasks /Query /TN "CodeTutor-CleanupTraces"`（查看）、`/Run`（手动跑一次）、`/Delete /F`（删除）。

---

## Roadmap（详细）

详细的炸场功能优先级与设计文档见本地 `docs/` 目录（设计评审用，未纳入 git 追踪）。

> 简历向一句话标题：
> **CodeTutor: A Self-Verifying, Long-Term Adaptive Coding Mentor with Closed-Loop Tutoring and Edit-Trace Observability**

---

## License

本项目以 [MIT License](LICENSE) 开源。详见 [LICENSE](LICENSE) 文件。

---

## Author

Andre

> ⚠️ 项目处于活跃开发阶段。API / State schema / 节点拓扑仍可能演进，但核心闭环（出题 → 判题 → 辅导 → 轨迹分析 → 规划出题）已可端到端跑通。
