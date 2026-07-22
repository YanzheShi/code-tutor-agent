# Issues（本地跟踪，未接入 GitHub Issues，因 `gh` 未安装）

---

## #1 出题进度提示：继续出题显示后端步骤列表，初始出题不显示

**状态**：待处理（待确认是否要统一展示）
**类型**：前端展示一致性
**报告日期**：2026-07-22

### 现象

点击「继续出题」时，会弹出一个全屏遮罩，逐条罗列后端的出题进度：

```
正在准备下一题…
回到与导师的对话…
正在调用大模型生成题目…
第 1/1 次尝试 — 生成中…
🧪 正在解析示例测试用例…
✅ 题目已就绪！
🧪 正在生成更多测试用例...
🔧 正在运行暴力解验证 12 个用例...
🤖 正在生成边界测试用例...
```

但在**初始出题**（创建会话后第一次生成题目）时，看不到这一长串步骤，基本只闪一下 loading 就进入做题界面。

### 已定位的根因

后端在两条路径都会把进度写进全局 `_generation_progress[sid]`，并经 SSE 重放（`stream_progress` 先推已存在的快照），所以**数据两边都有**。差异在前端展示时机：

1. **继续出题**（`callNextProblem`，`frontend/src/hooks/useSession.ts`）：
   - 设 `nextProblemLoading=true`，用 `MainLayout.tsx:214-223` 的全屏遮罩承载；
   - 该遮罩**显式 `map` 渲染整列 `progressMsgs`**，且只在 `/next-problem` POST 返回 `data.problem` 时才关闭；
   - EventSource（SSE）一直开着，所以连 `problem` 就绪**之后**才推的"更多测试用例/暴力解/边界用例"（后台 `asyncio.create_task` 推送）也能收进来。

2. **初始出题**（`handleStart` → `startProgress` → `LoadingScreen`）：
   - `stream_progress` 一旦检测到 `problem` 存在就立刻推 `done`（`session.py:397`，只看 `problem` 是否存在）；
   - 前端 `onDone` 回调（`useSession.ts:81-82`）**立即 `setProgressMsgs([])` 并 `setScreen('main')`**，把列表清空、卸载 LoadingScreen；
   - 那些在 `problem` 就绪**之后**才推的复杂用例消息（generation.py:114、208 的后台 task）到达时，前端早已切走，收不到。

结论：初始出题"看不到"是因为 `done`→清空的机制把进度截短了；继续出题的遮罩生命周期绑在 POST 往返上、且故意罗列全量步骤，所以完整列表才显示出来。

### 拟议修复（两条路，尚未实施）

- **路子 A（推荐，纯前端 + 延迟关闭）**：初始出题也走 `nextProblemLoading` 遮罩（`MainLayout.tsx:214-223` 可复用），`onDone` 收到后**不立即关闭 SSE、也不清空列表**，等一个短宽限期让后台复杂用例消息推完，再 `closeProgress()` + 清 `progressMsgs`。改动小、不影响"题目就绪即可做题"。
- **路子 B（后端重排）**：把复杂用例生成挪到 `problem` 标记就绪**之前**（`run_generation` 里先生成完再 push "题目已就绪"），使 `done` 发出时消息已齐。但会**拉长首题出现时延**，不推荐。

### 相关文件

- `frontend/src/hooks/useSession.ts`（`handleStart` / `startProgress.onDone` / `callNextProblem`）
- `frontend/src/components/MainLayout.tsx`（214-223 遮罩）
- `frontend/src/components/LoadingScreen.tsx`（25-29 已 map `progressMsgs`）
- `src/code_tutor_agent/api/routers/session.py`（`stream_progress` 397 行 `done` 判定）
- `src/code_tutor_agent/api/services/generation.py`（`run_generation` 104-114，复杂用例后台 task）
