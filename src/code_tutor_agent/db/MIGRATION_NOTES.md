# DB 模型化改造 — 重构记录

> 2026-07-10 完成 Phase 1 修复

## 已修复

| # | 问题 | 状态 | 改动文件 |
|---|------|------|---------|
| 1 | constraints 字段加到 DB | ✅ | `db/models.py` + `db/database.py` + `api/services/generation.py` |
| 2 | agent_judge.py 测试 mock 改返回 DBProblem | ✅ | `tests/test_agent_judge_node.py` + `nodes/agent_judge.py` |
| 4 | adversarial.py 类型标注 + 属性访问 | ✅ | `sandbox/adversarial.py` + `tests/test_d3_adversarial.py` |
| 5 | admin.py 列表接口改用属性访问 | ✅ | `api/routers/admin.py` |
| 6 | get_submissions_by_problem 改用 DBSubmission | ✅ | `db/models.py` + `db/database.py` |

## 未修复（与本次改造无关）

| # | 问题 | 原因 |
|---|------|------|
| 3 | `test_agent_dialog_node::test_first_visit_does_not_duplicate` | 预存在，`agent_dialog_history` 被清空，与 DB 改造无关 |

## DBProblem.get() bridge 方法

`DBProblem` 上保留了 `.get(key, default)` 和 `__getitem__` 方法用于迁移过渡。
在 `nodes/agent_judge.py` 和 `sandbox/adversarial.py` 中已改用属性访问。
剩余使用 `.get()` 的场景：`sandbox/adversarial.py` 中 `generate_scale_adversarial()` 的 `problem_dict` 仍是 dict 类型（来自 LLM 输出，非 DB）。