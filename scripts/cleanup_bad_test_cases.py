"""清理库中 expected_output 为空的坏测试用例。

根因：早期后台生成（api/services/generation.py 旧逻辑）会把参考解报错信息 /
空串当 expected 入库；且旧版随机生成器会造出非法输入（如非矩形随机字符串网格）。
判题 harness 又会拿空 expected 误判 WA。

本脚本对任意「expected_output 为空」的用例：
  1. 用题目自带参考解（optimal_solution / brute_solution）重跑该用例的 input_args，
     借 harness 在空 expected 时回传真实输出的特性，拿到权威 expected；
  2. 参考解能跑通 -> 回填 expected（保留这条用例的覆盖）；
  3. 参考解也崩（RE/TLE/无输出）-> 说明 input 本身也是垃圾，直接丢弃。

加 --regen-inputs 时，会先用**已修复**的随机生成器为坏用例重生一份
合法输入（如矩形的 0/1 网格），再用参考解补 expected——彻底替换历史垃圾输入，
而不只是回填 expected。

用法：
  uv run python scripts/cleanup_bad_test_cases.py                 # 仅回填 expected
  uv run python scripts/cleanup_bad_test_cases.py --regen-inputs # 连输入一起重生
  uv run python scripts/cleanup_bad_test_cases.py --dry-run      # 只预览，不改库
  uv run python scripts/cleanup_bad_test_cases.py --limit 5      # 只处理前 N 题
"""
from __future__ import annotations

import argparse
import json
import sys

# 让脚本可直接从仓库根目录运行，无需安装包。
sys.path.insert(0, "src")

from code_tutor_agent.db.database import (
    get_all_problem_ids,
    get_problem_by_id,
    update_problem_test_cases,
)
from code_tutor_agent.sandbox.runner import run_solution
from code_tutor_agent.sandbox.input_generator import generate_random_inputs

# 参考解无法跑通的状态 —— 这些意味着 input 本身有问题，应丢弃。
_DROP_STATUSES = {"Runtime Error", "TLE", "Judge Error"}


def _regenerate_input(func_sig: str, seed: int) -> list[str] | None:
    """用修复后的随机生成器造一份合法 input_args；失败返回 None。"""
    if not func_sig:
        return None
    try:
        out = generate_random_inputs(func_sig, count=1, seed=seed)
        if out:
            return out[0]
    except Exception as exc:  # 生成失败（如签名无法解析）-> 不重生
        print(f"    [WARN] 重生输入失败（sig={func_sig!r}）: {exc}")
    return None


def _refill_one(
    code: str,
    tc: dict,
    timeout: float,
    regen: bool = False,
    func_sig: str = "",
    seed: int = 0,
) -> tuple[bool, dict | None, str]:
    """对单条空 expected 用例重跑参考解，返回 (keep, final_tc, reason)。

    keep=True 时 final_tc 为已填好 expected 的用例（regen 时输入也已重生）；
    keep=False 时 reason 说明为何丢弃。
    """
    candidate = dict(tc)

    # --regen-inputs：先用修复后的生成器造合法输入，替换历史垃圾输入。
    if regen:
        new_args = _regenerate_input(func_sig, seed)
        if new_args is not None:
            candidate["input_args"] = new_args
            candidate["explanation"] = "重新生成的合法输入（替换历史垃圾输入）"
        # 若重生失败，退化为保留原输入、仅回填 expected。

    probe = dict(candidate)
    probe["expected_output"] = ""  # 空 expected -> harness 回传实际输出
    try:
        results = run_solution(code, [probe], timeout=timeout)
    except Exception as exc:  # 极端兜底：任何异常都当作丢弃
        return False, None, f"harness exception: {exc}"

    if not results:
        return False, None, "no result"

    r = results[0]
    if r.status in _DROP_STATUSES:
        return False, None, f"ref {r.status}: {r.detail}"
    new_expected = (r.actual_output or r.detail or "").strip()
    if not new_expected:
        return False, None, "ref produced empty output"

    candidate["expected_output"] = new_expected
    return True, candidate, new_expected


def _is_empty_expected(tc: dict) -> bool:
    exp = tc.get("expected_output")
    return exp is None or (isinstance(exp, str) and exp.strip() == "")


def cleanup(
    dry_run: bool,
    limit: int | None,
    timeout: float,
    regen_inputs: bool = False,
) -> dict:
    ids = get_all_problem_ids()
    if limit is not None:
        ids = ids[:limit]

    summary = {
        "problems_scanned": 0,
        "problems_with_bad_cases": 0,
        "bad_cases_found": 0,
        "cases_refilled": 0,
        "cases_dropped": 0,
        "problems_updated": 0,
        "problems_skipped_empty": 0,
    }

    for pid in ids:
        summary["problems_scanned"] += 1
        prob = get_problem_by_id(pid)
        if not prob:
            continue

        tcs = prob.test_cases
        if not tcs:
            continue

        bad = [tc for tc in tcs if _is_empty_expected(tc)]
        if not bad:
            continue

        summary["problems_with_bad_cases"] += 1
        summary["bad_cases_found"] += len(bad)

        code = prob.optimal_solution or prob.brute_solution
        if not code:
            print(f"[SKIP] problem #{pid} '{prob.title}': 无参考解，无法回填，需手动/重生成")
            summary["problems_skipped_empty"] += 1
            continue

        new_tcs = []
        for idx, tc in enumerate(tcs):
            if not _is_empty_expected(tc):
                new_tcs.append(tc)
                continue
            keep, final_tc, val = _refill_one(
                code, tc, timeout,
                regen=regen_inputs,
                func_sig=prob.function_signature,
                seed=pid * 1000 + idx,
            )
            if keep:
                new_tcs.append(final_tc)
                summary["cases_refilled"] += 1
                tag = "REGEN" if regen_inputs else "REFILL"
                print(f"[{tag}] #{pid} '{prob.title}': expected <- {val[:60]}")
            else:
                summary["cases_dropped"] += 1
                print(f"[DROP]   #{pid} '{prob.title}': {val[:60]}")

        if not new_tcs:
            print(f"[WARN] #{pid} '{prob.title}': 所有用例均被丢弃，跳过更新以免库空")
            continue

        if dry_run:
            print(f"[DRY-RUN] #{pid} '{prob.title}': 将更新为 {len(new_tcs)} 条用例（不改库）")
        else:
            update_problem_test_cases(pid, new_tcs)
            summary["problems_updated"] += 1

    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="清理 expected 为空的坏测试用例")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 题")
    ap.add_argument("--timeout", type=float, default=10.0, help="参考解单用例超时（秒）")
    ap.add_argument(
        "--regen-inputs", action="store_true",
        help="用修复后的生成器重生合法输入（替换历史垃圾输入），而不只是回填 expected",
    )
    args = ap.parse_args()

    print("=" * 60)
    print(f"坏用例清理  dry_run={args.dry_run}  regen_inputs={args.regen_inputs}"
          f"  limit={args.limit}  timeout={args.timeout}s")
    print("=" * 60)

    summary = cleanup(args.dry_run, args.limit, args.timeout, args.regen_inputs)

    print("-" * 60)
    print("汇总:")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print("=" * 60)


if __name__ == "__main__":
    main()
