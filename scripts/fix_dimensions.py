"""统一补全/规范化各 topic 维度 JSON（data/problem-dimension/*.json）。

职责（按用户 2026-08-27「先生成、后面统一处理」要求）：
- 读取每个 topic 的维度原始文件（可能是模型直出、含 ```json 围栏、
  尾逗号、字段命名不一致，甚至截断）。
- robust 解析：剥 markdown 围栏 → 截取首个 [ 到末个 ] → 去尾逗号 →
  字段规整为 {dimension_name, description, example_directions}。
- 规整后原地覆盖为合法标准 JSON（ensure_ascii=False, indent=2）。
- 报告：原本 OK / 已修复 / 仍失败需人工。

对截断等无法修复的情况，标记 remain_broken 并打印片段，不强制覆盖（保留原文）。

运行：
  env -u PYTHONPATH .venv/Scripts/python.exe scripts/fix_dimensions.py
  env -u PYTHONPATH .venv/Scripts/python.exe scripts/fix_dimensions.py --topics 栈 队列
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIM_DIR = os.path.join(ROOT, "data", "problem-dimension")


def robust_parse(raw: str):
    """返回 (list|None, status)。status: 'ok'|'fixed'|'broken'。"""
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    # 先尝试直接解析
    try:
        data = json.loads(s)
        return data, "ok"
    except json.JSONDecodeError:
        pass
    # 截取数组
    a, b = s.find("["), s.rfind("]")
    if a == -1 or b == -1 or b < a:
        return None, "broken"
    s2 = s[a:b + 1]
    s2 = re.sub(r",\s*([}\]])", r"\1", s2)  # 去尾逗号
    try:
        data = json.loads(s2)
        return data, "fixed"
    except json.JSONDecodeError:
        return None, "broken"


def normalize(data) -> list[dict]:
    """把任意结构规整成标准维度列表。"""
    if not isinstance(data, list):
        # 容忍 {"dimensions": [...]} 或 {"data": [...]}
        if isinstance(data, dict):
            for k in ("dimensions", "data", "items", "list"):
                if isinstance(data.get(k), list):
                    data = data[k]
                    break
        if not isinstance(data, list):
            raise ValueError("非数组结构")
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("dimension_name") or item.get("name") or \
            item.get("dimension") or ""
        desc = item.get("description") or item.get("desc") or \
            item.get("detail") or ""
        ex = item.get("example_directions") or item.get("examples") or \
            item.get("directions") or []
        if not isinstance(ex, list):
            ex = [str(ex)]
        ex = [str(x).strip() for x in ex if str(x).strip()]
        if not name:
            continue
        out.append({
            "dimension_name": name,
            "description": desc,
            "example_directions": ex,
        })
    return out


def main():
    ap = argparse.ArgumentParser(description="补全/规范化维度 JSON")
    ap.add_argument("--topics", nargs="*", default=None,
                    help="指定 topic；缺省处理 DIM_DIR 下全部 *.json")
    args = ap.parse_args()

    if args.topics:
        files = [os.path.join(DIM_DIR, f"{t}.json") for t in args.topics]
    else:
        files = sorted(
            os.path.join(DIM_DIR, f) for f in os.listdir(DIM_DIR)
            if f.endswith(".json"))

    n_ok = n_fixed = n_broken = 0
    broken_list = []
    for fp in files:
        if not os.path.exists(fp):
            print(f"  [skip] 不存在: {fp}", flush=True)
            continue
        name = os.path.basename(fp)
        raw = open(fp, encoding="utf-8").read()
        data, status = robust_parse(raw)
        if status == "broken" or not data:
            n_broken += 1
            broken_list.append(name)
            print(f"  ❌ {name}: 无法解析（可能截断），保留原文待人工 "
                  f"| 片段: {raw[:80]!r}", flush=True)
            continue
        try:
            norm = normalize(data)
        except Exception as exc:
            n_broken += 1
            broken_list.append(name)
            print(f"  ❌ {name}: 规整失败 {exc}", flush=True)
            continue
        if not norm:
            n_broken += 1
            broken_list.append(name)
            print(f"  ❌ {name}: 解析后无有效维度", flush=True)
            continue
        # 原地覆盖为标准格式
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(norm, f, ensure_ascii=False, indent=2)
        if status == "ok":
            n_ok += 1
            print(f"  ✅ {name}: 原本合法，已标准化（{len(norm)} 维）",
                  flush=True)
        else:
            n_fixed += 1
            print(f"  🔧 {name}: 已修复（{len(norm)} 维）", flush=True)

    print(f"\n完成：原本 OK {n_ok}，已修复 {n_fixed}，仍失败 {n_broken}。",
          flush=True)
    if broken_list:
        print(f"需人工处理的文件：{broken_list}", flush=True)


if __name__ == "__main__":
    main()
