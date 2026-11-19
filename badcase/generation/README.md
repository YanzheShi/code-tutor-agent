# 出题 badcase 回归样本

机器可读的真实失败样本，用于回归「出题校验闸 / 修复回路」的改动。
面向人的复盘文档仍在 `docs/badcases.md`；本目录只放**可被测试直接消费**的 JSON。

## 样本索引

| ID | 题目 | 缺陷类型 | 可修 | 修复锚点 | 该由哪道闸拦 |
|---|---|---|---|---|---|
| BC-GEN-2026-11-18-01 | 最长覆盖目标价的连续交易日 | 规格矛盾 + 参考解错（无序数组上二分）+ 偶然通过 | partial（需先裁决语义） | 无（双解各实现一条互斥规则） | sandbox_gate |
| BC-GEN-2026-11-18-02 | 游戏资源背包：最小手动存储次数 | 输出被 CoT 注释耗尽、函数无 return、示例里泄漏思考过程 | no（应重采样） | 无（双解都错） | static_gate |
| BC-GEN-2026-11-18-03 | 古籍检索块定位 | 边界 bug：空隙查询返回上一个块号而非 -1 | yes（1 轮） | `brute_solution`（两例全过） | sandbox_gate |

> 「修复锚点」：双解交叉验证里有没有一个能跑通示例的正确参考。有锚点时修复是「对齐到已知正确解」，
> 没有锚点时要么先裁决语义（01），要么直接重采样（02）。

来源：`backend-logs-2026-11-18.log`，`request_id = a3170cbb-695`，同一会话连续 3 次原创题失败后降级到 LeetCode（pid=74）。

## Schema（schema_version = 1）

```jsonc
{
  "schema_version": 1,
  "id": "BC-GEN-YYYY-MM-DD-NN",
  "created_at": "YYYY-MM-DD",
  "source": {
    "log": "日志文件名",
    "request_id": "…",
    "session_id": "…",
    "timestamp": { "attempt_start": "ISO8601", "rejected_at": "ISO8601" },
    "topic": "二分查找",
    "difficulty_requested": "medium",
    "attempt": 1,                 // 同一次出题里的第几次尝试
    "injection_mode": "F",        // 方案 H 注入
    "llm_ms": 10729,              // 该次 LLM 墙钟
    "validation_ms": 52
  },
  "problem": { /* LLM 原始产物，逐字段保留，不做清洗 */ },
  "observed": {
    "stage": "examples",
    "verdict": "rejected",
    "pipeline_cases_executed": 1, // 线上实际跑了几个示例就 return None
    "pipeline_first_failure": { "index": 0, "status": "Wrong Answer", "expected": "5", "actual": "0" },
    "offline_full_scan": [        // 离线复跑全部示例（登记隐藏缺陷）
      { "index": 0, "input_args": ["…"], "stated": "5", "status": "Wrong Answer", "actual": "0" }
    ],
    "cross_check": {              // 双解交叉验证：判断「有没有修复锚点」
      "brute_present": true,
      "brute_scan": [ { "index": 0, "status": "Passed", "actual": "5" } ],
      "disagreement_with_optimal": true,
      "anchor_available": "brute | none",
      "supports": "为什么这组交叉结果能/不能支撑修复"
    },
    "offline_note": "…"
  },
  "diagnosis": {
    "defect_codes": ["SPEC_CONTRADICTION"],
    "severity": "blocking",
    "root_cause": "…",
    "evidence": ["…"],
    "repairable": "yes | partial | no",
    "repair_difficulty": "easy | hard | unrepairable",
    "resample_preferred": false,  // true = 有 draft 也不该修，直接重采样
    "repair_strategy": ["…"],
    "note": "…"
  },
  "regression": {
    "must_reject_by": ["static_gate | sandbox_gate"],
    "must_not_save": true,
    "expected_outcome": "static_gate_reject | sandbox_gate_reject"
  }
}
```

约定：

- `problem` 必须原样保留，**不要**修正示例或解法——样本的价值就是复现失败。
- `offline_full_scan` 是离线复跑结果，不等于线上执行过的用例数；两者差值本身是「首错即停」的证据。
- `must_reject_by` 用于未来闸门的断言：静态闸能拦的，不应该浪费一次沙箱调用或一轮修复。

## 修复 vs 重采样的判据（从这三份样本反推）

| 信号 | 含义 | 处置 |
|---|---|---|
| `anchor_available = brute` | 双解里有一个能跑通示例 | 高置信修复：把另一解对齐锚点，1 轮 |
| 双解都不通过、但互相矛盾且各自对应描述里的一条规则 | 规格矛盾 | 先裁决语义，再改解 + 改示例，≤2 轮 |
| 双解都不通过、且 draft 截断/注释占比 > 60% | 整体理解失败 | 不修，静态闸判废 + 重采样 |

结论：**「有 draft」不等于「可修」**。缺陷密度、锚点可用性、截断与否，三者决定该修还是该重抽。

## 静态闸（2026-11-19 已落地）

`guards/code_quality.py` 已接入两个入口（`agent_problem.verify_problem` 与
`generation.verifier.CodeVerifier`），02 号样本现在会在**进入 runner 之前**被拒：

- `class Solution` 入口方法没有 `return`（含「只在嵌套函数里 return」）→ 判废；
- 注释占比 > 70% 且可执行代码 < 10 行 → 判废（防用思考注释填满输出预算）；
- CoT 扫描扩到 `examples` 与英文短语；示例解释 > 500 字 → 判废。

预期行为：**01 / 03 仍然通过静态闸**——它们是语义矛盾与边界逻辑错误，
静态看代码是合法的，必须靠沙箱发现。静态闸只负责「省掉无意义的沙箱调用」。

回测（2026-11-18 日志里的 4 道真实题）：

| 题 | 静态闸 | 说明 |
|---|---|---|
| 城市交通信号灯（pid=73，成功） | 通过 | 无误杀 |
| 最长覆盖目标价（01） | 通过 | 交给沙箱闸 |
| 游戏资源背包（02） | **拒绝**（5 条缺陷） | 无 return + 注释 96% + 英文 CoT + 示例含推理 |
| 古籍检索块定位（03） | 通过 | 交给沙箱闸 |

## 使用

```bash
# 只跑 badcase 回归（本地沙箱，不调 LLM / Judge0）
pytest tests/test_generation_badcases.py -q

# 全量回归
pytest -m "not integration" --ignore=tests/integration
```

## 新增样本

1. 从日志里捞 `Full problem dict`（DEBUG 行）+ 对应的 `参考解自验证失败` WARNING；
2. 用 `_parse_examples_to_test_cases` + `run_solution` 离线复跑全部示例；
3. 补齐 `diagnosis`：缺陷码、根因、可修性、修复策略；
4. 跑一遍 badcase 测试，确认 `offline_full_scan` 与实测一致。
