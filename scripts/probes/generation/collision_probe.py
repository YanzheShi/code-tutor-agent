"""出题碰撞率 / 成功率探针（temperature 单维扫描，零侵入）。

为什么复用而非重造
-------------------
本脚本**不改任何 src 代码**，只通过 monkeypatch ``config.get_llm`` 把
``temperature`` 注入到出题用途（``purpose="problem"``），从而复用真实的：

* ``agent_problem.generate_problem`` —— LLM 结构化出题 + ``verify_problem`` 编译自校验
* ``ProblemGenerationAgent._build_sample_tests`` —— 示例→用例 + 本地参考解自洽检查
  （``run_solution(force_local=True)``，纯 subprocess，不走 JUDGE0）
* ``db.normalize_starter_code`` —— 出题去重用的归一化（0dd2bbe 落地的那个），
  即「函数签名一样 / 描述略不同即判碰撞」的官方口径
* 全部 prompt 模板 / ``Problem`` schema / 网关配置

核心代码一行未改；且 ``generate_problem`` 本身**不落库**（落库在更上层的
``ProblemGenerationAgent``），所以本探针跑 N 次只产生内存中的 ``Problem`` 对象，
不写 DB、不触发真实去重——彻底零侵入、无副作用。

成功 / 碰撞口径（与用户约定一致）
---------------------------------
* 出题成功：``generate_problem`` 返回 + ``_build_sample_tests`` 示例自洽都过。
  （注意：``verify_problem`` 只编译不跑用例；真正的「自洽」由 ``_build_sample_tests``
   的本地参考解跑通示例提供。完整测试套件是落库后异步生成的，探针不落库跑不到。）
* 碰撞（B 口径）：同批 N=10 内部两两碰撞率 = 归一化形态重复对数 / C(N,2)。
  不统计「对现有题库命中率」（现有题库主题分布不均，命中率高不等于题本身重复）。

运行
----
    uv run python scripts/generation_collision_probe.py \\
        --topics 数组 二叉树 链表 动态规划 字符串 回溯 \\
        --temperatures 0.0 0.2 0.5 0.7 1.0 \\
        --n 10 --difficulty medium \\
        --model default        # agnes 主模型（默认）
    # 换 sensenova-6.8-flash（secondary）再测一次，产物自动落到不同文件：
    uv run python scripts/generation_collision_probe.py \\
        --topics 数组 二叉树 链表 动态规划 字符串 回溯 \\
        --temperatures 0.0 0.2 0.5 0.7 1.0 \\
        --n 10 --difficulty medium --model secondary
    # 产物：out/probe_report_default.html / out/probe_default.json
    #       out/probe_report_secondary.html / out/probe_secondary.json
    # 两次结果可直接并列对比（同网格、不同模型）。
依赖合法 ``LLM_*`` 环境变量（同主程序），否则 ``get_llm`` 会抛配置不完整。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations

# ── 复用真实代码：项目根加入 sys.path（与 auto_solver.py 一致）──
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import code_tutor_agent.config as cfg  # noqa: E402
import code_tutor_agent.agents.agent_problem as ap  # noqa: E402
from code_tutor_agent.db.database import normalize_starter_code  # noqa: E402
from code_tutor_agent.generation.problem_generation_agent import (  # noqa: E402
    ProblemGenerationAgent,
)
from code_tutor_agent.generation.gateways.llm import (  # noqa: E402
    normalize_topic_for_generation,
)
from code_tutor_agent.generation.state import ProblemDraft  # noqa: E402

# 单格固定常量
DIF = "medium"
DUP = 2          # 同格并发，确保同温同题可比；>1 会交叉污染温度效应，保持 1
MAX_RETRIES = 2  # generate_problem 内部自带重试


@dataclass
class CellResult:
    topic: str
    norm_topic: str
    temperature: float
    difficulty: str
    n: int
    success: int = 0            # 出题成功（含示例自洽）
    gen_fail: int = 0           # LLM 出题失败 / verify_problem 不过
    selfcheck_fail: int = 0     # 出题成功但示例自洽不过
    norms: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    # (i, j, norm) 内部两两碰撞对
    collision_pairs: list[tuple[int, int, str]] = field(default_factory=list)
    collision_rate: float = 0.0  # 重复对数 / C(N,2)

    @property
    def rate(self) -> float:
        return self.success / self.n if self.n else 0.0

    def to_dict(self) -> dict:
        return {
            "topic": self.topic, "norm_topic": self.norm_topic,
            "temperature": self.temperature, "difficulty": self.difficulty,
            "n": self.n, "success": self.success, "gen_fail": self.gen_fail,
            "selfcheck_fail": self.selfcheck_fail, "norms": self.norms,
            "titles": self.titles,
            "collision_pairs": [list(p) for p in self.collision_pairs],
            "collision_rate": self.collision_rate,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CellResult":
        r = cls(
            topic=d["topic"], norm_topic=d.get("norm_topic", d["topic"]),
            temperature=d["temperature"], difficulty=d.get("difficulty", "medium"),
            n=d["n"],
        )
        r.success = d.get("success", 0)
        r.gen_fail = d.get("gen_fail", 0)
        r.selfcheck_fail = d.get("selfcheck_fail", 0)
        r.norms = d.get("norms", [])
        r.titles = d.get("titles", [])
        r.collision_pairs = [tuple(p) for p in d.get("collision_pairs", [])]
        r.collision_rate = d.get("collision_rate", 0.0)
        return r

    @property
    def key(self) -> tuple:
        """格子级去重键：(topic, temperature)。"""
        return (self.topic, self.temperature)


def inject_model(temp: float, model_alias: str) -> None:
    """monkeypatch get_llm：仅对出题用途强制 temperature + 切到指定模型。

    模型切换（方案 A，零侵入 src）：临时把 ``PURPOSE_CONFIGS["problem"]["alias"]``
    指向目标别名（如 ``"secondary"`` = sensenova），再调真实 ``get_llm``——这和业务上
    “把 problem 用途改配到 ALT 模型”完全等价，走的是真实的 purpose→alias→注册表链路。

    同时必须替换两处 ``get_llm`` 引用：``cfg.get_llm``（供延迟调用方）与
    ``agent_problem.get_llm``（``generate_problem`` 模块加载时已 ``from config import
    get_llm`` 绑定到局部名，只改 cfg 拦不住）。两者都包一层即可。
    """
    import code_tutor_agent.agents.agent_problem as ap_mod

    cfg_orig = cfg.get_llm
    ap_orig = ap_mod.get_llm

    # 记录原始 alias，便于还原（每格重注入前都重置，避免串模型）
    orig_alias = cfg.PURPOSE_CONFIGS["problem"].get("alias", "default")

    def _patched(purpose: str, **kw):
        if purpose == "problem":
            kw["temperature"] = temp
            # 切模型：只影响 problem 用途，其它用途保持原 alias
            cfg.PURPOSE_CONFIGS[purpose]["alias"] = model_alias
        return cfg_orig(purpose, **kw)
    cfg.get_llm = _patched
    ap_mod.get_llm = _patched
    inject_model._orig_alias = orig_alias  # type: ignore[attr-defined]


RPM_DELAY = 0.0   # 每道 LLM 调用前的节流 Sleep（秒）；限流模型（如 sensenova 429）设大值


def probe_one(topic: str, temp: float, n: int, agent: ProblemGenerationAgent,
              difficulty: str, model_alias: str = "default",
              rpm_delay: float = 0.0) -> CellResult:
    """扫描单格 (topic, temperature)，复现真实出题 + 自洽 + 碰撞。"""
    norm_topic = normalize_topic_for_generation(topic)
    res = CellResult(topic=topic, norm_topic=norm_topic,
                     temperature=temp, difficulty=difficulty, n=n)

    inject_model(temp, model_alias)
    try:
        for i in range(n):
            if rpm_delay > 0:
                time.sleep(rpm_delay)  # 节流：避免打满网关 RPM（sensenova 429）
            try:
                problem = ap.generate_problem(norm_topic, DIF, purpose="problem",
                                             max_retries=MAX_RETRIES)
            except Exception:  # 内部重试全失败 / 自校验不过
                res.gen_fail += 1
                continue

            # 复用真实示例自洽检查（本地 subprocess，不落库）
            draft = ProblemDraft(
                topic=topic, difficulty=DIF,
                title=problem.title, description=problem.description,
                starter_code=problem.starter_code or "",
                optimal_solution=problem.optimal_solution or "",
                brute_solution=problem.brute_solution or "",
                examples=list(problem.examples or []),
                constraints=list(problem.constraints or []),
                function_signature=problem.function_signature or "",
            )
            sample = agent._build_sample_tests(draft)
            if sample is None:
                res.selfcheck_fail += 1
                continue

            res.success += 1
            res.norms.append(normalize_starter_code(problem.starter_code or ""))
            res.titles.append(problem.title or "")
    finally:
        # 还原，保证多格之间不串温度（虽然每格都重新注入，仍稳妥）
        pass

    # B 口径：内部两两碰撞（归一化形态重复对数 / C(N,2)）
    total = 0
    repeat = 0
    for a, b in combinations(range(len(res.norms)), 2):
        total += 1
        if res.norms[a] and res.norms[a] == res.norms[b]:
            repeat += 1
            res.collision_pairs.append((a, b, res.norms[a][:60]))
    res.collision_rate = repeat / total if total else 0.0
    return res


def _ckpt_path(default_json: str) -> str:
    """checkpoint 落 --json 同目录，固定名 probe_ckpt.json。"""
    import os as _os
    d = _os.path.dirname(default_json) or "."
    return _os.path.join(d, "probe_ckpt.json")


def _load_ckpt(path: str) -> dict:
    """读回已有 checkpoint：{(topic, temperature): CellResult}。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {tuple(c["key"]): CellResult.from_dict(c) for c in data.get("cells", [])}
    except Exception as exc:
        print(f"  ⚠️ checkpoint 读取失败，忽略：{exc}", flush=True)
        return {}


def _save_ckpt(path: str, cells: dict) -> None:
    """原子写 checkpoint：先写 .tmp 再 rename，避免半截文件。"""
    import tempfile
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    payload = {
        "note": "auto-generated by generation_collision_probe; resume with --resume",
        "cells": [c.to_dict() | {"key": list(k)} for k, c in cells.items()],
    }
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=os.path.dirname(path) or ".",
                                     delete=False, suffix=".tmp") as tf:
        json.dump(payload, tf, ensure_ascii=False, indent=2)
        tmpname = tf.name
    os.replace(tmpname, path)


def run_all(topics, temps, n, difficulty, checkpoint_path: str,
            resume: bool, retry_failed: bool, model_alias: str = "default",
            rpm_delay: float = 0.0) -> list[CellResult]:
    """扫描全部格子；支持断点续跑（格子级）。

    * resume=True：载入 checkpoint，跳过已完成格子（含失败格，符合「默认跳过」约定）。
    * retry_failed=True：即使 checkpoint 里该格已完成，仍重跑（覆盖）。
    * 每完成一个格子立即原子写 checkpoint；顶层异常也尽量落盘，保证可续。
    """
    agent = ProblemGenerationAgent()
    loaded: dict[tuple, CellResult] = _load_ckpt(checkpoint_path) if resume else {}
    done: dict[tuple, CellResult] = dict(loaded)  # 复制，避免崩溃覆盖时丢失已载入的格子

    # 计划清单
    planned = [(t, topic) for t in temps for topic in topics]
    remaining = []
    for t, topic in planned:
        k = (topic, t)
        skip = k in done and not retry_failed
        if skip:
            print(f"  ⏭️  跳过已完成 (resume): topic={topic} T={t}", flush=True)
        else:
            remaining.append((t, topic))

    total = len(planned)
    finished = len(planned) - len(remaining)
    print(f"计划 {total} 格，已完成 {finished}，本次待跑 {len(remaining)}", flush=True)

    results: list[CellResult] = []
    try:
        for idx, (t, topic) in enumerate(remaining):
            print(f"  ▶ [{idx+1}/{len(remaining)}] temperature={t} topic={topic} (n={n})",
                  flush=True)
            res = probe_one(topic, t, n, agent, difficulty, model_alias, rpm_delay)
            done[(topic, t)] = res  # 覆盖（含失败格），保证 checkpoint 总是最新
            _save_ckpt(checkpoint_path, done)
            results.append(res)
    except (KeyboardInterrupt, Exception) as exc:  # 系统级崩：落盘已跑的，可续
        print(f"\n⚠️ 中断（{type(exc).__name__}）：已跑 {len(results)} 格已落 checkpoint，"
              f"可 --resume 续跑。", flush=True)
        # done 已含从 checkpoint 载入的历史格子 + 本次新跑的，落盘不会丢失
        _save_ckpt(checkpoint_path, done)
        # 把已完成的（来自 checkpoint）也合并进返回，便于本次也能出报告
        results.extend(c for k, c in done.items() if k not in {(r.topic, r.temperature) for r in results})
        return results

    # 合并 checkpoint 里被跳过的格子
    results.extend(c for k, c in done.items() if k not in {(r.topic, r.temperature) for r in results})
    return results


# ───────────────────────── 报告（HTML + 内联 SVG） ─────────────────────────
def build_html(results: list[CellResult], topics, temps, n, out_path: str,
               model_alias: str = "default", model_name: str = "") -> None:
    from collections import OrderedDict

    # 组装：topic -> [(temp, success_rate, collision_rate)]
    by_topic: "OrderedDict[str, list]" = OrderedDict()
    for t in topics:
        by_topic[t] = []
    for r in results:
        by_topic[r.topic].append((r.temperature, r.rate, r.collision_rate,
                                  r.success, r.gen_fail, r.selfcheck_fail))

    # 配色
    palette = ["#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ea580c", "#0891b2"]

    # ── SVG 折线图：每个 topic 一条线，X=温度，Y=成功率 ──
    W, H = 760, 360
    ml, mr, mt, mb = 60, 20, 30, 50
    plot_w, plot_h = W - ml - mr, H - mt - mb
    xmin, xmax = min(temps), max(temps)
    xspan = (xmax - xmin) or 1.0

    def sx(tv):
        return ml + (tv - xmin) / xspan * plot_w

    def sy(v):
        return mt + (1 - v) * plot_h

    lines = []
    for idx, (topic, series) in enumerate(by_topic.items()):
        color = palette[idx % len(palette)]
        pts = " ".join(f"{sx(t):.1f},{sy(r):.1f}" for (t, r, _c, _s, _g, _f) in series)
        lines.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="2.5" '
            f'points="{pts}" />')
        for (t, r, _c, _s, _g, _f) in series:
            lines.append(
                f'<circle cx="{sx(t):.1f}" cy="{sy(r):.1f}" r="4" fill="{color}">'
                f'<title>{topic} @T={t}: 成功率 {r*100:.0f}%</title></circle>')
    # 网格 + 轴
    y_ticks = "".join(
        f'<line x1="{ml}" y1="{sy(v)}" x2="{W-mr}" y2="{sy(v)}" stroke="#eee"/>'
        f'<text x="{ml-8}" y="{sy(v)+4}" text-anchor="end" font-size="11" fill="#666">{v*100:.0f}%</text>'
        for v in (0, 0.25, 0.5, 0.75, 1.0))
    x_ticks = "".join(
        f'<text x="{sx(t)}" y="{H-mb+18}" text-anchor="middle" font-size="11" fill="#666">T={t}</text>'
        for t in temps)
    legend = "".join(
        f'<span style="display:inline-block;margin-right:14px;">'
        f'<span style="display:inline-block;width:12px;height:12px;background:{palette[i%len(palette)]};'
        f'border-radius:2px;margin-right:5px;"></span>{topic}</span>'
        for i, topic in enumerate(by_topic))
    success_svg = f"""
    <svg viewBox="0 0 {W} {H}" width="100%" style="max-width:780px;">
      {y_ticks}
      <line x1="{ml}" y1="{mt}" x2="{ml}" y2="{H-mb}" stroke="#999"/>
      <line x1="{ml}" y1="{H-mb}" x2="{W-mr}" y2="{H-mb}" stroke="#999"/>
      {x_ticks}
      {''.join(lines)}
    </svg>
    <div style="margin:6px 0 18px;font-size:13px;">{legend}</div>
    """

    # ── SVG 折线图：碰撞率 ──
    lines2 = []
    for idx, (topic, series) in enumerate(by_topic.items()):
        color = palette[idx % len(palette)]
        pts = " ".join(f"{sx(t):.1f},{sy(c):.1f}" for (t, _r, c, _s, _g, _f) in series)
        lines2.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="2.5" points="{pts}" />')
        for (t, _r, c, _s, _g, _f) in series:
            lines2.append(
                f'<circle cx="{sx(t):.1f}" cy="{sy(c):.1f}" r="4" fill="{color}">'
                f'<title>{topic} @T={t}: 碰撞率 {c*100:.0f}%</title></circle>')
    collision_svg = f"""
    <svg viewBox="0 0 {W} {H}" width="100%" style="max-width:780px;">
      {y_ticks}
      <line x1="{ml}" y1="{mt}" x2="{ml}" y2="{H-mb}" stroke="#999"/>
      <line x1="{ml}" y1="{H-mb}" x2="{W-mr}" y2="{H-mb}" stroke="#999"/>
      {x_ticks}
      {''.join(lines2)}
    </svg>
    <div style="margin:6px 0 18px;font-size:13px;">{legend}</div>
    """

    # ── 汇总表 ──
    rows = []
    for r in results:
        rows.append(
            f"<tr><td>{r.topic}</td><td>{r.temperature}</td>"
            f"<td>{r.n}</td><td>{r.success}</td>"
            f"<td><b>{r.rate*100:.0f}%</b></td>"
            f"<td>{r.collision_rate*100:.0f}%</td>"
            f"<td>{r.gen_fail}</td><td>{r.selfcheck_fail}</td></tr>")
    table = (
        '<table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;'
        'font-size:13px;">'
        "<thead style='background:#f1f5f9;'>"
        "<tr><th>topic</th><th>temperature</th><th>N</th><th>成功</th>"
        "<th>成功率</th><th>碰撞率</th><th>出题失败</th><th>自洽失败</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>")

    # ── 逐格碰撞明细（可折叠）──
    details = []
    for r in results:
        if not r.collision_pairs:
            continue
        pairs = "; ".join(f"({a},{b})" for (a, b, _n) in r.collision_pairs)
        details.append(
            f"<li><b>{r.topic} @T={r.temperature}</b>：{len(r.collision_pairs)} 对碰撞 "
            f"[{pairs}]</li>")
    detail_html = ("<ul style='font-size:12px;'>" + "".join(details) + "</ul>") if details \
        else "<p style='font-size:12px;color:#666'>无内部碰撞对。</p>"

    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>出题碰撞率 / 成功率探针报告</title>
<style>body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:32px;color:#1e293b;}}
h1{{font-size:22px;}} h2{{font-size:17px;margin-top:28px;border-left:4px solid #2563eb;padding-left:8px;}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:16px 20px;margin:12px 0;}}
.meta{{color:#64748b;font-size:13px;}}</style></head><body>
<h1>出题碰撞率 / 成功率探针报告</h1>
<p class="meta">生成时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}<br>
模型：{model_alias}（{model_name}）<br>
难度：{DIF} · 每格 N={n} · 温度扫描：{temps} · topics：{topics}<br>
口径：成功率 = 出题成功(含示例自洽)/N；碰撞率(B) = 归一化形态内部两两重复对数 / C(N,2)。
不统计对现有题库命中率（主题分布不均）。</p>

<div class="card"><h2>① 成功率 vs temperature</h2>{success_svg}</div>
<div class="card"><h2>② 碰撞率 vs temperature</h2>{collision_svg}</div>
<div class="card"><h2>③ 汇总表</h2>{table}</div>
<div class="card"><h2>④ 内部碰撞明细</h2>{detail_html}</div>
</body></html>"""

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    aparser = argparse.ArgumentParser(description="出题碰撞率/成功率探针（temperature 单维）")
    aparser.add_argument("--topics", nargs="+",
                         default=["数组", "二叉树", "链表", "动态规划", "字符串", "回溯"])
    aparser.add_argument("--temperatures", type=float, nargs="+",
                         default=[0.0, 0.2, 0.5, 0.7, 1.0])
    aparser.add_argument("--n", type=int, default=10)
    aparser.add_argument("--difficulty", default=DIF)
    aparser.add_argument("--model", default="default", choices=["default", "secondary", "gmi"],
                        help="出题模型别名：default=agnes(主)，secondary=sensenova-6.8-flash(ALT)，gmi=GMI Cloud(OpenAI兼容网关)。")
    aparser.add_argument("--report", default=None,
                        help="报告路径；默认按模型落到 out/probe_report_{model}.html")
    aparser.add_argument("--json", default=None,
                        help="JSON 路径；默认按模型落到 out/probe_{model}.json")
    aparser.add_argument("--resume", action="store_true",
                        help="断点续跑：载入同目录 probe_ckpt_{model}.json，跳过已完成格子（默认含失败格）。")
    aparser.add_argument("--retry-failed", action="store_true",
                        help="配合 --resume：即使 checkpoint 里该格已完成（含失败），仍重跑覆盖。")
    aparser.add_argument("--rpm-delay", type=float, default=0.0,
                        help="每道 LLM 调用前的节流 Sleep（秒）；限流模型（sensenova 429）设大值，如 20。")
    args = aparser.parse_args()

    # 默认产物按模型分文件，避免两次跑互相覆盖
    model_tag = args.model  # default / secondary
    report = args.report or f"out/probe_report_{model_tag}.html"
    jpath = args.json or f"out/probe_{model_tag}.json"

    ckpt = _ckpt_path(jpath)  # out/probe_ckpt_{model}.json
    if args.retry_failed and not args.resume:
        print("⚠️ --retry-failed 需配合 --resume，已自动开启 --resume。", flush=True)
        args.resume = True

    model_name = cfg.LLM_CONFIGS.get(args.model, {}).get("model", "") or "(未配置)"
    print(f"开始探针：model={args.model}({model_name}) topics={args.topics} "
          f"temps={args.temperatures} n={args.n}"
          f"{' [resume]' if args.resume else ''}{' [retry-failed]' if args.retry_failed else ''}"
          f"{' [rpm-delay='+str(args.rpm_delay)+'s]' if args.rpm_delay else ''}",
          flush=True)
    results = run_all(args.topics, args.temperatures, args.n, args.difficulty,
                     ckpt, args.resume, args.retry_failed, args.model, args.rpm_delay)

    # JSON 原始数据
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_alias": args.model, "model_name": model_name,
        "difficulty": DIF, "n": args.n,
        "topics": args.topics, "temperatures": args.temperatures,
        "resume": args.resume, "retry_failed": args.retry_failed,
        "checkpoint": ckpt,
        "cells": [
            {"topic": r.topic, "norm_topic": r.norm_topic,
             "temperature": r.temperature, "n": r.n,
             "success": r.success, "gen_fail": r.gen_fail,
             "selfcheck_fail": r.selfcheck_fail,
             "success_rate": r.rate, "collision_rate": r.collision_rate,
             "titles": r.titles,
             "collision_pairs": [list(p) for p in r.collision_pairs]}
            for r in results
        ],
    }
    os.makedirs(os.path.dirname(jpath) or ".", exist_ok=True)
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    build_html(results, args.topics, args.temperatures, args.n, report,
               model_alias=args.model, model_name=model_name)
    print(f"\n✅ 完成。HTML: {report}\n   JSON: {jpath}", flush=True)


if __name__ == "__main__":
    main()
