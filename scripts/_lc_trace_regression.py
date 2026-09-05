"""LeetCode URL 导入 → 解题 → run/submit → 编辑轨迹 → 轨迹分析 全链路回归脚本。

与 auto_solver.py 互补：auto_solver 只覆盖 generator/pool 出题，本脚本专测
「对话消息贴 URL」的 agent-only 导入契约 + 轨迹分析四端点。

验证点：
  1. URL 必须经 POST /session/{sid}/chat/stream 以对话消息发出才触发导入
  2. 判题：run(可见用例) + submit(全量)
  3. 编辑轨迹：edit/run/submit 三类事件按 problem_id 隔离落库
  4. 轨迹分析：POST /analyze（首轮）→ GET /analysis（读取）→ POST /analyze（追问）
     → POST /analyze/summarize（过渡压缩，双落点）

用法：
  python scripts/_lc_trace_regression.py                       # 默认两道题
  python scripts/_lc_trace_regression.py <url1> <url2> ...     # 指定题目

沙箱/隔离环境提示：WorkBuddy 沙箱对项目目录内文件删除有 ~30s/次拦截，
SQLite WAL 连接关闭（删 -wal）会撞上，全链路会随机 database is locked。
先复制库到系统 Temp，再用环境变量指过去（默认路径行为不变）：
  CTA_DB_PATH=<Temp>/code_tutor.db CHECKPOINT_DB_PATH=<Temp>/checkpoints.db
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from auto_solver import TutorClient, SolverLLM, _now_ms  # noqa: E402

BASE = "http://localhost:8765"
DEFAULT_URLS = [
    "https://leetcode.cn/problems/coin-change/",
    "https://leetcode.cn/problems/longest-substring-without-repeating-characters/",
]
MAX_ATTEMPTS = 2
FOLLOWUP = "我在这道题上的思维卡点是什么？给一条最该改的编码习惯。"


def send_chat(sid: str, message: str) -> None:
    """经对话消息发送内容（LeetCode 导入的唯一入口），消费完 SSE。"""
    with requests.post(
        f"{BASE}/session/{sid}/chat/stream",
        json={"message": message},
        stream=True,
        timeout=(10, 300),
    ) as resp:
        for _ in resp.iter_lines():
            pass


def run_one(client: TutorClient, llm: SolverLLM, url: str) -> dict:
    rec: dict = {"url": url, "attempts": 0}
    t0 = time.time()
    sid = client.create_session(None, None)
    client.wait_for_dialog(sid)
    send_chat(sid, f"{url} 我想做这道 LeetCode 题，直接帮我导入开始做。")
    problem = client.wait_ready(sid)
    pid = problem.get("problem_id")
    rec.update({
        "session_id": sid,
        "pid": pid,
        "title": problem.get("title"),
        "difficulty": problem.get("difficulty"),
        "n_visible": len(problem.get("visible_test_cases") or []),
        "acquire_sec": round(time.time() - t0, 2),
    })
    print(f"  📝 导入完成 《{rec['title']}》 pid={pid} 可见用例 {rec['n_visible']} 条")

    feedback = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        rec["attempts"] = attempt
        code = llm.generate(problem, feedback)
        client.emit_trace(
            sid,
            [{"type": "edit",
              "change": "初次生成解法" if attempt == 1 else f"按判题反馈修正(第{attempt}次)",
              "ts": _now_ms()}],
            problem_id=pid,
        )
        run_resp = client.run(sid, code)
        client.emit_trace(sid, [{"type": "run", "ts": _now_ms()}], problem_id=pid)
        rec["run_all_passed"] = run_resp.get("all_passed")
        sub_resp = client.submit(sid, code)
        client.emit_trace(sid, [{"type": "submit", "ts": _now_ms()}], problem_id=pid)
        verdict = sub_resp.get("verdict")
        rec["verdict"] = verdict
        print(f"  🔁 第{attempt}次: run.all_passed={rec['run_all_passed']} submit.verdict={verdict}")
        if verdict == "AC":
            rec["ac"] = True
            break
        feedback = f"提交未通过（{verdict}）。请针对失败用例修正，输出完整 Python class Solution。"
    else:
        rec["ac"] = False

    # ── 轨迹分析全链路（AC 与否都跑，验证端点健壮性）──
    traj: dict = {}
    try:
        an = client.analyze(sid, pid)
        an_res = an.get("analysis") or an
        traj["analyze_ok"] = True
        traj["has_change_path"] = bool(an_res.get("change_path"))
        traj["has_weakness_tags"] = bool(an_res.get("weakness_tags"))
        traj["autonomy"] = an_res.get("autonomy")
        traj["n_changes"] = len(an_res.get("change_path") or [])
        traj["n_tags"] = len(an_res.get("weakness_tags") or [])
        traj["weakness_tags"] = an_res.get("weakness_tags") or []
        print(f"  🧠 /analyze OK: 变化节点 {traj['n_changes']} 个 / 弱点标签 {traj['n_tags']} 个 "
              f"/ autonomy={traj['autonomy']}")
    except Exception as e:  # noqa: BLE001
        traj["analyze_ok"] = False
        traj["analyze_error"] = str(e)
        print(f"  ❌ /analyze 失败: {e}")

    try:
        ga = client.get_analysis(sid, pid)
        traj["get_analysis_ok"] = True
        traj["stored_change_path_len"] = len((ga.get("analysis") or ga).get("change_path") or [])
        print(f"  📖 /analysis 读取 OK（落库 change_path {traj['stored_change_path_len']} 节点）")
    except Exception as e:  # noqa: BLE001
        traj["get_analysis_ok"] = False
        traj["get_analysis_error"] = str(e)
        print(f"  ❌ /analysis 失败: {e}")

    try:
        fu = client.analyze_followup(sid, pid, FOLLOWUP)
        traj["followup_ok"] = bool(fu and str(fu).strip())
        traj["followup_preview"] = str(fu)[:200]
        print(f"  💬 追问 OK（{len(str(fu))} 字）")
    except Exception as e:  # noqa: BLE001
        traj["followup_ok"] = False
        traj["followup_error"] = str(e)
        print(f"  ❌ 追问失败: {e}")

    try:
        sm = client.summarize(sid, pid, transition_action="continue")
        traj["summarize_ok"] = True
        traj["summary_preview"] = json.dumps(sm, ensure_ascii=False)[:200]
        print("  🗜️  /analyze/summarize OK（过渡压缩双落点）")
    except Exception as e:  # noqa: BLE001
        traj["summarize_ok"] = False
        traj["summarize_error"] = str(e)
        print(f"  ❌ summarize 失败: {e}")

    rec["trajectory"] = traj
    return rec


def main() -> None:
    urls = sys.argv[1:] or DEFAULT_URLS
    client = TutorClient(BASE)
    llm = SolverLLM(
        model=os.getenv("LLM_MODEL", "deepseek-v4-flash"),
        base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
        api_key=os.environ["LLM_API_KEY"],
    )
    if not client.health():
        print(f"❌ 后端不可达：{BASE}")
        sys.exit(1)
    print(f"✅ 后端健康：{BASE}")

    results = []
    for url in urls:
        print(f"\n=== {url} ===")
        try:
            results.append(run_one(client, llm, url))
        except Exception as e:  # noqa: BLE001
            print(f"❌ 链路异常：{e}")
            results.append({"url": url, "error": str(e)})

    out = ROOT / "out" / "reg_20260904_leetcode_trace.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n📄 报告落盘：{out}")
    for r in results:
        flag = "AC" if r.get("ac") else f"非AC({r.get('verdict') or r.get('error')})"
        traj = r.get("trajectory") or {}
        print(f"  - {r.get('title') or r.get('url')}: {flag} | "
              f"analyze={traj.get('analyze_ok')} analysis={traj.get('get_analysis_ok')} "
              f"followup={traj.get('followup_ok')} summarize={traj.get('summarize_ok')}")


if __name__ == "__main__":
    main()
