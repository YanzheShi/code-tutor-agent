"""一次性修复：为 problem id=2 重新生成合法的随机测试用例。

背景：
    problem 2 = LeetCode 2134「环形数组中的最少交换次数」(minSwaps)，
    签名 `nums: List[int] -> int`，约束 `0 <= nums[i] <= 1`。
    旧随机生成器因中文约束无法解析 + 数组元素假值-0 bug，回退到
    [-1000,1000]，生成非法大整数 → 最优解 IndexError → 12 个随机用例
    全部被丢弃（最终只剩 2 可见 + 8 LLM 边界 = 10 条）。

本脚本：
    1. 重新解析约束 → 值区间 [0,1]
    2. 用修复后的生成器重新产出 N 个 0/1 随机输入
    3. 用 optimal + brute 参考解对每个输入做交叉验证（force_local 子进程）
    4. 把通过验证的随机用例合并进现有 test_cases_json（保留既有已验证用例）
    5. 写回前先做备份

用法：
    python scripts/backfill_problem2_tc.py            # dry-run：只打印，不写库
    python scripts/backfill_problem2_tc.py --write    # 真正写回（先备份）
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)

from code_tutor_agent.sandbox.input_generator import (  # noqa: E402
    generate_random_inputs,
    _parse_constraint_ranges,
)
from code_tutor_agent.sandbox.runner import run_solution  # noqa: E402

DB_PATH = os.path.join(HERE, "..", "data", "db", "code_tutor.db")
PROBLEM_ID = 2
DROP_STATUSES = {"Runtime Error", "TLE", "Judge Error"}
RANDOM_COUNT = 30


def main(write: bool) -> int:
    if not os.path.exists(DB_PATH):
        print(f"[error] DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    backup = f"{DB_PATH}.bak_backfill_p{PROBLEM_ID}"
    if not os.path.exists(backup):
        shutil.copy2(DB_PATH, backup)
        print(f"[backup] -> {backup}")
    else:
        print(f"[backup] already exists: {backup} (skipping copy)")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM problems WHERE id = ?", (PROBLEM_ID,)).fetchone()
    if not row:
        print(f"[error] problem {PROBLEM_ID} not found", file=sys.stderr)
        return 1

    constraints = json.loads(row["constraints_json"] or "[]")
    func_sig = row["function_signature"]
    optimal = row["optimal_solution"]
    brute = row["brute_solution"]
    existing_tcs = json.loads(row["test_cases_json"] or "[]")
    visible_raw = row["visible_test_cases_json"]

    print(f"[read] title={row['title']!r}")
    print(f"[read] signature={func_sig!r}")
    print(f"[read] constraints={constraints}")
    print(f"[read] existing test cases = {len(existing_tcs)}")
    print(f"[read] existing visible_test_cases_json len = "
          f"{len(json.loads(visible_raw)) if visible_raw else 0}")

    # 1) 校验约束解析
    mv, Mv, ml, Ml = _parse_constraint_ranges(constraints)
    print(f"[parse] val=[{mv},{Mv}] len=[{ml},{Ml}]")
    if (mv, Mv) != (0, 1):
        print(f"[error] expected val range (0,1) but got ({mv},{Mv})", file=sys.stderr)
        return 2

    # 2) 生成随机输入
    random_inputs = generate_random_inputs(
        func_sig, count=RANDOM_COUNT, constraints=constraints, seed=20260813,
    )
    elems: set = set()
    for args in random_inputs:
        for a in args:
            try:
                v = ast.literal_eval(a)
            except Exception:
                continue
            elems.update(v) if isinstance(v, list) else elems.add(v)
    print(f"[gen] {len(random_inputs)} inputs; element values seen = {sorted(elems)}")
    if not elems <= {0, 1}:
        print(f"[error] illegal elements: {sorted(elems - {0, 1})}", file=sys.stderr)
        return 3

    # 3) 交叉验证（双解一致才保留）
    new_tcs: list[dict] = []
    dropped = 0
    for idx, inp in enumerate(random_inputs):
        tc = {
            "input_args": inp,
            "expected_output": "",
            "is_hidden": True,
            "explanation": f"随机生成测试(回填) {idx + 1}",
        }
        opt_r = run_solution(optimal, [tc], function_signature=func_sig, force_local=True)
        brute_r = run_solution(brute, [tc], function_signature=func_sig, force_local=True)
        if not opt_r or not brute_r:
            dropped += 1
            continue
        o, b = opt_r[0], brute_r[0]
        if o.status in DROP_STATUSES or b.status in DROP_STATUSES:
            dropped += 1
            continue
        od, bd = (o.detail or "").strip(), (b.detail or "").strip()
        if not od or not bd or od != bd:
            dropped += 1
            continue
        tc["expected_output"] = od
        new_tcs.append(tc)

    print(f"[xval] random: generated {len(random_inputs)}, kept {len(new_tcs)}, "
          f"dropped {dropped}")

    merged = list(existing_tcs) + new_tcs
    visible_merged = [tc for tc in merged if not tc.get("is_hidden")]

    print(f"[merge] total test cases -> {len(merged)} "
          f"(existing {len(existing_tcs)} + new random {len(new_tcs)})")
    print(f"[merge] visible subset -> {len(visible_merged)}")

    if not write:
        print("\n[dry-run] 未写入数据库。加 --write 才真正落库。")
        conn.close()
        return 0

    conn.execute(
        "UPDATE problems SET test_cases_json = ?, visible_test_cases_json = ? WHERE id = ?",
        (json.dumps(merged, ensure_ascii=False),
         json.dumps(visible_merged, ensure_ascii=False),
         PROBLEM_ID),
    )
    conn.commit()
    conn.close()
    print(f"\n[write] problem {PROBLEM_ID} 已写回：总 {len(merged)} 条"
          f"（备份：{backup}）")
    return 0


if __name__ == "__main__":
    do_write = "--write" in sys.argv[1:]
    sys.exit(main(do_write))
