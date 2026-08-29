#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_solver.py — CodeTutor Agent「连续做题」自动化测试驱动（黑盒，零侵入）

设计原则（对业务代码零侵入）：
  1. 不 import 任何 src/code_tutor_agent 业务模块，只通过 HTTP 驱动运行中的
     FastAPI 服务（默认 http://localhost:8765）。
  2. 做题代码由 LLM 生成，复用 .env 中的 LLM_* 网关（OpenAI 兼容），
     不修改 config.py / get_llm。
  3. 全程走对外接口，模拟真实用户：出题 → 生成代码 → 运行(可见用例) → 提交(全量判题)，
     单轮内可多次「运行 + 提交」模拟连续做题，WA 时把失败用例回灌 LLM 重生成。

忠实模拟真实做题流程（v2 重点）：
  ✅ 画像更新：真实 POST /submit（full 提交）的 graph 链路是
     agent_judge → update_profile_node → critic_node，AC 时必然写 v2 能力画像
     （prof/stab/forget/errors/attempts）+ flush problem_history + 触发语义记忆抽取。
     画像按 user_id="default" 存储，跨轮自动累积。本工具在开始前/后抓取
     GET /admin/profile/v2 做前后对比，证明画像确实更新。
     （此外，若开启 --emit-traces，submit 时还会触发错误模式画像 feeder，
      把 edit_trace 增量写入 DBProfile.error_modes。）
  ✅ 上下文压缩：连续模式（--session-strategy continuous，默认）多轮复用同一 session，
     换题时调用 POST /session/{sid}/next-problem（agent 模式重新进入对话），
     服务端用 build_cross_problem_context + generate_summary 把历史压缩成
     context_summary 注入下一题对话——这正是真实用户「连续做题」的上下文压缩路径。
  ✅ 轨迹分析（新架构·按题隔离）：每轮 AC 后调用 POST /session/{sid}/analyze，
     按 problem_id 隔离读取本题 edit_traces + 终码，LLM 产出 change_path /
     weakness_tags / autonomy 等独立复盘（独立线程、不回灌画像）。
     连续模式再调用 POST /session/{sid}/analyze/summarize 做「过渡压缩（双落点）」：
     把本题分析线程压成 ≤500 字/10 条摘要落库——下一步 next-problem 会读取它并
     注入下一题 context_summary，从而把「轨迹分析」与「上下文压缩」真正接通。
  ✅ 编辑轨迹采集：做题过程中按前端契约 POST /session/{sid}/edit-trace 发送
     edit/run/submit 事件（每个事件带 problem_id 防串题），使轨迹分析有真实数据可分析
     （与前端行为一致，纯 HTTP）。

出题两种模式（仅影响「第一题」如何获得）：
  --mode generator : 真实出题链路（POST /session → chat/stream 推对话完成 → 后台出题）
  --mode pool      : GET /problems + POST /session/by-problem/{id}（确定性，零出题成本）
                     注：连续模式下，第一题可用 pool 播种，之后的题均走 next-problem
                     （agent 对话重新出题），因为 next-problem 必然经过出题对话。

判题契约（来自 sandbox/runner.py，必须匹配否则代码判 WA）：
  - 用户代码被包进 harness，取 `class Solution` 的第一个 public 方法执行：
        Solution().method(*args)
  - args 来自 visible_test_cases[].input_args（JSON 字符串解析）
  - 返回值按值比较（list→JSON，set→排序 JSON，其他→str）与 expected_output 比对
  - 故生成的代码必须是 LeetCode 风格 `class Solution` + 单一 public 方法，
    签名/返回值匹配 starter_code / 可见用例。

用法：
  # 默认：连续 5 轮（单会话，含画像更新/上下文压缩/轨迹分析），每轮最多 2 次提交
  python scripts/auto_solver.py --rounds 5

  # 仅校验连通性 + 出题契约（不出题求解，快速验证部署）
  python scripts/auto_solver.py --validate-only

  # 指定主题/难度（generator 模式；每轮对话都沿用，模拟「集中练一个方向」）
  python scripts/auto_solver.py --rounds 3 --topic 动态规划 --difficulty medium

  # 不要轨迹分析/编辑轨迹（仅测 AC 率与延迟）
  python scripts/auto_solver.py --rounds 5 --no-traces

  # 每轮新建独立会话（不压缩上下文，便于隔离单题行为做对照）
  python scripts/auto_solver.py --rounds 5 --session-strategy fresh

  # 服务没起时自动拉起（uv run uvicorn）
  python scripts/auto_solver.py --rounds 5 --auto-start

  # 报告落盘
  python scripts/auto_solver.py --rounds 5 --report data/auto_solver_report.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import random
import datetime
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    print("❌ 缺少依赖 requests，请先安装：pip install requests")
    sys.exit(1)


# ───────────────────────────────────────────────────────────
#  .env 加载（优先 dotenv，缺失则极简手动解析，避免引入硬依赖）
# ───────────────────────────────────────────────────────────
def load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
        return
    except Exception:
        pass
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


load_env()


# ───────────────────────────────────────────────────────────
#  HTTP 客户端（黑盒驱动 FastAPI）
# ───────────────────────────────────────────────────────────
class TutorClient:
    """对运行中的 CodeTutor Agent 服务做黑盒 HTTP 调用。"""

    def __init__(self, base_url: str, timeout: float = 300.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()

    # ── 基础 ──
    def health(self) -> bool:
        try:
            r = self.s.get(f"{self.base}/health", timeout=10)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def _post(self, path: str, json_body: dict | None = None) -> dict:
        r = self.s.post(f"{self.base}{path}", json=json_body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _get(self, path: str) -> dict:
        r = self.s.get(f"{self.base}{path}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ── 出题（generator 模式）──
    def create_session(self, topic: str | None, difficulty: str | None) -> str:
        # 注意：agent 模式下即便传了 topic/difficulty，POST /session 也只会进入
        # 「导师对话」态（status=dialog），不会直接出题。真正的出题要靠
        # complete_dialog() 把对话推到完成（见 chat router 的 agent_dialog 分支）。
        body = {}
        if topic:
            body["topic"] = topic
        if difficulty:
            body["difficulty"] = difficulty
        data = self._post("/session", body or None)
        return data["session_id"]

    def wait_for_dialog(self, sid: str, timeout: float = 30.0) -> None:
        """等待会话进入 dialog 态（建会话后的后台 graph.invoke 会进入对话）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                st = self._get(f"/session/{sid}/state")
            except requests.RequestException:
                time.sleep(0.5)
                continue
            if st.get("status") == "dialog":
                return
            if st.get("problem"):  # 极少数情况直接出好题了
                return
            time.sleep(0.8)
        raise RuntimeError(f"等待 dialog 态超时（{timeout}s）：sid={sid}")

    def complete_dialog(self, sid: str, topic: str | None, difficulty: str | None) -> None:
        """驱动导师对话到完成，触发真实出题。

        向 /session/{sid}/chat/stream 发送一条明确含 topic/difficulty 的消息，
        analyze_user_intent 判定就绪后会在服务端置 agent_dialog_complete=True 并
        后台调度 planner→generator 出题。需完整消费 SSE 流，保证后台任务被调度。
        """
        msg = self._dialog_message(topic, difficulty)

        def _send(message: str) -> None:
            resp = self.s.post(
                f"{self.base}/session/{sid}/chat/stream",
                json={"message": message},
                stream=True,
                timeout=(10, 180),
            )
            resp.raise_for_status()
            for _ in resp.iter_lines(decode_unicode=True):
                pass

        _send(msg)
        st = self._get(f"/session/{sid}/state")
        if st.get("status") == "dialog":
            _send("别追问了，直接给我出一道题，不要确认。")
            st = self._get(f"/session/{sid}/state")
            if st.get("status") == "dialog":
                raise RuntimeError(
                    f"无法完成导师对话（出题前置）：sid={sid}，"
                    f"请检查 analyze_user_intent / LLM 可用性"
                )

    @staticmethod
    def _dialog_message(topic: str | None, difficulty: str | None) -> str:
        if topic and difficulty:
            return (f"请直接给我出一道关于「{topic}」方向、「{difficulty}」难度的算法题，"
                    f"不用确认，直接开始出题。")
        if topic:
            return f"请直接给我出一道关于「{topic}」方向的算法题，不用确认，直接出题。"
        if difficulty:
            return f"请直接给我出一道「{difficulty}」难度的算法题，不用确认，直接出题。"
        return "请随机给我出一道算法题，不用确认，直接开始出题。"

    def wait_ready(self, sid: str, timeout: float = 240.0) -> dict:
        """等题目就绪，返回 problem dict。优先 SSE，失败回退轮询 /state。"""
        try:
            resp = self.s.get(
                f"{self.base}/session/{sid}/progress/stream",
                stream=True,
                timeout=(10, timeout + 10),
            )
            if resp.status_code == 200:
                for event, payload in _iter_sse(resp):
                    if event == "error":
                        raise RuntimeError(f"出题失败(SSE): {payload}")
                    if event == "done":
                        try:
                            state = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        problem = state.get("problem")
                        if problem:
                            return problem
        except requests.RequestException:
            pass

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state = self._get(f"/session/{sid}/state")
            except requests.RequestException:
                time.sleep(1.0)
                continue
            problem = state.get("problem")
            if problem:
                return problem
            status = state.get("status")
            if status == "error":
                raise RuntimeError(f"出题失败(state): {state.get('error_message', '')}")
            time.sleep(0.8)
        raise RuntimeError(f"等待题目就绪超时（{timeout}s）：sid={sid}")

    # ── 出题（pool 模式）──
    def list_problems(self) -> list[dict]:
        data = self._get("/problems")
        return data.get("problems", [])

    def by_problem(self, pid: int) -> dict:
        return self._post(f"/session/by-problem/{pid}")

    # ── 运行 / 提交 ──
    def run(self, sid: str, code: str) -> dict:
        return self._post(f"/session/{sid}/run", {"code": code, "language": "python"})

    def submit(self, sid: str, code: str) -> dict:
        return self._post(f"/session/{sid}/submit", {"code": code, "language": "python"})

    def state(self, sid: str) -> dict:
        return self._get(f"/session/{sid}/state")

    # ── 换题（连续模式：触发上下文压缩）──
    def next_problem(self, sid: str, preference: str = "continue_dialog") -> dict:
        """换到下一题（agent 模式重新进入对话）。

        服务端会基于已 flush 的 problem_history 构建 context_summary（跨题摘要 +
        本提对话摘要），完成「上下文压缩」后重新进入 dialog 态。返回 NextProblemResp。
        """
        return self._post(f"/session/{sid}/next-problem",
                          {"preference": preference})

    # ── 编辑轨迹采集（与前端契约一致，使轨迹分析有真实数据）──
    def emit_trace(self, sid: str, events: list[dict], problem_id: str | None = None) -> None:
        """发送一批编辑轨迹事件（edit/idle/run/submit），按题隔离。

        problem_id 透传：请求体级 + 逐事件级都带上，保证连续多题时事件不串题
        （服务端 get_edit_trace_by_problem 按 events_json 内的 problem_id 过滤）。
        非致命：失败绝不影响主流程。
        """
        if not events:
            return
        pid = str(problem_id) if problem_id is not None else None
        body = {"events": events}
        if pid is not None:
            body["problem_id"] = pid
            for e in events:
                if isinstance(e, dict) and not e.get("problem_id"):
                    e["problem_id"] = pid
        try:
            self._post(f"/session/{sid}/edit-trace", body)
        except requests.RequestException as e:
            # 轨迹采集失败绝不影响主流程
            print(f"    ⚠️ 编辑轨迹采集失败（已忽略）: {e}")

    # ── 轨迹分析（AC 后复盘，按题隔离、独立线程、可多轮追问）──
    def analyze(self, sid: str, problem_id: str = "default") -> dict:
        """首轮结构化分析（无 message）。返回含 analysis 字段的 dict。

        响应形态（新 trace 模块）：
          无 message → {"ok", "session_id", "problem_id", "analysis": {...AnalysisResult...}}
          有 message → {"ok", "session_id", "problem_id", "reply": "<自由文本>"}（此处不用）
        """
        pid = str(problem_id) if problem_id not in (None, "default") else "default"
        return self._post(f"/session/{sid}/analyze", {"problem_id": pid})

    def analyze_followup(self, sid: str, problem_id: str, message: str) -> str:
        """多轮追问：在同题分析线程追加问题，返回自由文本回复。非致命。"""
        pid = str(problem_id)
        try:
            data = self._post(f"/session/{sid}/analyze",
                              {"problem_id": pid, "message": message})
            return data.get("reply") or ""
        except requests.RequestException as e:
            print(f"    ⚠️ 轨迹分析追问失败（已忽略）: {e}")
            return ""

    def summarize(self, sid: str, problem_id: str, transition_action: str = "continue") -> dict:
        """过渡压缩（双落点）：AC 复盘后、换题前调用。

        把当前题分析线程压成 ≤500 字/10 条 TraceSummary 并落库；next-problem 会读取
        它并注入 context_summary。返回 {"ok", "summary": {...TraceSummary...}}。非致命。
        transition_action ∈ continue|next|change|abandon。
        """
        pid = str(problem_id)
        try:
            data = self._post(
                f"/session/{sid}/analyze/summarize",
                {"problem_id": pid, "transition_action": transition_action},
            )
            return data.get("summary") or {}
        except requests.RequestException as e:
            print(f"    ⚠️ 轨迹分析过渡压缩失败（已忽略）: {e}")
            return {}

    def get_analysis(self, sid: str, problem_id: str = "default") -> dict:
        """读取某题已缓存的首轮分析结论（problem_id 维度）。"""
        pid = str(problem_id) if problem_id not in (None, "default") else "default"
        return self._get(f"/session/{sid}/analysis?problem_id={pid}")

    # ── 画像快照（证明更新）──
    def get_profile_v2(self) -> dict | None:
        try:
            return self._get("/admin/profile/v2")
        except requests.RequestException:
            return None

    def get_profile_v1(self) -> dict | None:
        """读 v1 画像（proficiency/stability/attempts/common_errors）。免密 GET，非致命。"""
        try:
            return self._get("/admin/profile")
        except requests.RequestException:
            return None


def _iter_sse(resp):
    """解析 SSE 流，yield (event, data_json_string)。"""
    event, data_lines = None, []
    for raw in resp.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        line = raw
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif line.strip() == "":
            if event and data_lines:
                yield event, "\n".join(data_lines)
            event, data_lines = None, []


# ───────────────────────────────────────────────────────────
#  LLM 做题代码生成（OpenAI 兼容网关，复用 .env）
# ───────────────────────────────────────────────────────────
class SolverLLM:
    """调用 OpenAI 兼容 /chat/completions 生成 LeetCode 风格解题代码。"""

    SYSTEM = (
        "你是一名算法编程助手，专门解答 LeetCode 风格算法题。\n"
        "要求：\n"
        "1. 只输出一个 Python 代码块（```python ... ```），不要解释、不要示例、不要测试代码。\n"
        "2. 代码必须是 `class Solution:` 形式，且只包含一个 public 方法（方法名与题目给出的签名一致）。\n"
        "3. 如果题目提供了 starter_code（类/方法骨架），必须在其基础上补全，保持方法签名完全一致。\n"
        "4. 输入输出遵循 LeetCode 约定：数组用 list；链表/树已预定义 ListNode/TreeNode（可直接使用，不要重复定义）；"
        "返回值的比较按值进行（数组返回 list，字符串返回 str，集合返回排序后的 list）。\n"
        "5. 必须正确处理边界情况（空输入、单元素、负数、大数、重复值、越界等）。\n"
        "6. 时间/空间复杂度应尽量优（优先 O(n) 或 O(n log n)）。\n"
    )

    def __init__(self, model: str, base_url: str, api_key: str, temperature: float = 0.2,
                 max_tokens: int = 4096):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def generate(self, problem: dict, feedback: str | None = None) -> str:
        user = self._build_user_prompt(problem, feedback)
        messages = [{"role": "system", "content": self.SYSTEM},
                    {"role": "user", "content": user}]
        for attempt in range(1, 4):
            try:
                resp = requests.post(
                    self.url, headers=self.headers,
                    json={"model": self.model, "messages": messages,
                          "temperature": self.temperature, "max_tokens": self.max_tokens},
                    timeout=120,
                )
                if resp.status_code == 429 and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage") or {}
                self.total_prompt_tokens += usage.get("prompt_tokens", 0)
                self.total_completion_tokens += usage.get("completion_tokens", 0)
                content = data["choices"][0]["message"]["content"]
                code = _extract_code(content)
                if code:
                    return code
            except requests.RequestException as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"LLM 调用失败: {e}")
        raise RuntimeError("LLM 未返回可用代码")

    @staticmethod
    def _build_user_prompt(problem: dict, feedback: str | None) -> str:
        title = problem.get("title", "")
        topic = problem.get("topic", "")
        difficulty = problem.get("difficulty", "")
        description = (problem.get("description") or "").strip()
        constraints = problem.get("constraints") or []
        starter = (problem.get("starter_code") or "").strip()
        vcases = problem.get("visible_test_cases") or []

        lines = []
        lines.append(f"题目：{title}")
        lines.append(f"主题：{topic}    难度：{difficulty}")
        lines.append("")
        lines.append("题目描述：")
        lines.append(description or "（无描述）")
        if constraints:
            lines.append("")
            lines.append("约束：")
            for c in constraints:
                lines.append(f"  - {c}")
        if vcases:
            lines.append("")
            lines.append("可见测试用例（输入 -> 期望输出，仅供理解 I/O 格式，不要硬编码答案）：")
            for i, tc in enumerate(vcases[:5], 1):
                ins = tc.get("input_args", [])
                exp = tc.get("expected_output", "")
                lines.append(f"  {i}. 输入={ins} -> 期望={exp}")
        if starter:
            lines.append("")
            lines.append("题目提供的 starter_code（请在此骨架内补全，保持签名一致）：")
            lines.append("```python")
            lines.append(starter)
            lines.append("```")
        lines.append("")
        if feedback:
            lines.append("── 你上一版代码未通过判题，请根据失败用例修正后重新输出完整代码 ──")
            lines.append(feedback)
            lines.append("")
        lines.append("请直接给出求解代码（仅一个 ```python 代码块，不要任何解释）。")
        return "\n".join(lines)


def _extract_code(text: str) -> str:
    """从 LLM 回复中提取 Python 代码（去 markdown 围栏）。"""
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    if "class Solution" in text:
        return text.strip()
    return ""


def _build_feedback(problem: dict, run_resp: dict | None, state_resp: dict | None,
                    verdict: str | None) -> str:
    """汇总失败用例，构造回灌 LLM 的反馈文本。"""
    parts = []
    if verdict:
        parts.append(f"最终判题结果：{verdict}")
    if run_resp:
        fails = [r for r in (run_resp.get("results") or []) if not r.get("passed")]
        if fails:
            parts.append("可见用例失败明细：")
            for r in fails[:5]:
                parts.append(
                    f"  - 输入={r.get('input_args')} 期望={r.get('expected')} "
                    f"实际={r.get('actual')} 状态={r.get('status')} 细节={r.get('detail','')}"
                )
    if state_resp:
        subs = state_resp.get("submissions") or []
        if subs:
            last = subs[-1]
            jrs = last.get("judge_results") or []
            fails = [r for r in jrs if not r.get("passed")]
            if fails:
                parts.append("全量判题失败明细：")
                for r in fails[:8]:
                    parts.append(
                        f"  - 输入={r.get('input_args')} 期望={r.get('expected_output')} "
                        f"实际={r.get('actual_output')} 状态={r.get('status')}"
                    )
    return "\n".join(parts) if parts else "（无具体失败信息，请自查边界与逻辑）"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _run_trajectory_analysis(client: TutorClient, sid: str, problem_id: str,
                              do_summarize: bool = True,
                              followup: str | None = None) -> dict:
    """AC 后触发独立轨迹分析（按题隔离、可多轮追问、可过渡压缩）。

    真实流程对应关系：
      1. analyze（首轮结构化分析，读按题过滤的 edit_traces + 终码 → AnalysisResult）
      2. （可选）analyze_followup（多轮追问，返回自由文本）
      3. summarize（过渡压缩，双落点：可见卡 + 注入下一题 context_summary）
         仅在连续模式（do_summarize=True）调用——新鲜会话无接收方，跳过以省 LLM。
    全部非致命：任一环节失败只记录，不影响主流程。
    """
    out: dict = {"analysis": None, "followup": None, "summary": None}
    try:
        data = client.analyze(sid, problem_id)
        analysis = data.get("analysis")
        if analysis is None:
            out["error"] = "分析端点未返回结论（可能无编辑轨迹）"
        else:
            out["analysis"] = analysis
    except requests.RequestException as e:
        out["error"] = f"轨迹分析请求失败: {e}"
        return out

    if followup:
        try:
            out["followup"] = client.analyze_followup(sid, problem_id, followup)
        except requests.RequestException as e:
            out["followup_error"] = f"追问失败: {e}"

    if do_summarize:
        out["summary"] = client.summarize(sid, problem_id, transition_action="continue")
    return out


# ───────────────────────────────────────────────────────────
#  单轮：出题 → 连续运行/提交 → 轨迹分析 → 记录
# ───────────────────────────────────────────────────────────
def _acquire_fresh(client: TutorClient, mode: str, topic: str | None,
                   difficulty: str | None, used_pool_ids: set) -> tuple:
    """新建会话出题：pool 优先（确定性、零出题成本），题库耗尽回退 generator。

    返回 (problem, sid, via)，via ∈ {pool, generator, generator(fallback)}。
    连续模式换题失败时也复用本函数回退，保证多轮不中断。
    """
    if mode == "pool":
        problems = client.list_problems()
        avail = [p for p in problems if p.get("id") not in used_pool_ids]
        if avail:
            pick = random.choice(avail)
            used_pool_ids.add(pick["id"])
            state = client.by_problem(pick["id"])
            sid = state.get("session_id")
            problem = state.get("problem")
            if not problem:
                raise RuntimeError("by-problem 未返回题目")
            return problem, sid, "pool"
        print("  ⚠️ pool 题库已用尽，本轮回退 generator 出题")
    sid = client.create_session(topic, difficulty)
    client.wait_for_dialog(sid)
    client.complete_dialog(sid, topic, difficulty)
    problem = client.wait_ready(sid)
    return problem, sid, "generator" if mode != "pool" else "generator(fallback)"


def run_one_round(client: TutorClient, llm: SolverLLM, round_idx: int,
                  mode: str, topic: str | None, difficulty: str | None,
                  max_attempts: int, used_pool_ids: set, verbose: bool,
                  strategy: str = "continuous", sid: str | None = None,
                  emit_traces: bool = True, followup: str | None = None) -> dict:
    rec = {
        "round": round_idx,
        "mode": mode,
        "session_id": sid,
        "problem_id": None,
        "title": None,
        "topic": None,
        "difficulty": None,
        "acquired_via": None,
        "attempts": 0,
        "ac": False,
        "verdict": None,
        "acquire_sec": None,
        "gen_sec_total": 0.0,
        "run_sec_total": 0.0,
        "submit_sec_total": 0.0,
        "error": None,
        # v2 忠实模拟新增字段
        "context_compressed": False,      # 本轮回合用 next-problem 做了上下文压缩
        "recovered": False,               # 连续换题失败，已回退新建会话继续
        "problem_history_len": None,      # 进入本题前的历史题数（压缩的输入）
        "profile_updated": False,         # 本题 AC 触发了画像更新（submit 自动）
        "trajectory_analysis": None,      # AC 后首轮轨迹分析结论（按题隔离）
        "trajectory_summary": None,       # AC 后过渡压缩摘要（双落点，仅连续模式）
        "trajectory_followup": None,      # 可选多轮追问回复
    }

    # ── 1. 出题（含连续模式的上下文压缩）──
    t0 = time.time()
    try:
        if sid is not None and strategy == "continuous":
            # 连续模式：复用同一会话，换题时触发上下文压缩
            try:
                client.next_problem(sid)
                rec["context_compressed"] = True
                # 读压缩后的上下文（problem_history 累积 + 服务端已生成 context_summary）
                try:
                    st_after_np = client.state(sid)
                    rec["problem_history_len"] = len(st_after_np.get("problem_history") or [])
                except requests.RequestException:
                    pass
                client.wait_for_dialog(sid)
                client.complete_dialog(sid, topic, difficulty)
                problem = client.wait_ready(sid)
                rec["acquired_via"] = "next-problem(continuous)"
            except Exception as e:
                # 连续换题失败（典型：上一轮 WA 后会话卡在 dialog 态，
                # next-problem→complete_dialog 无法再触发出题）。回退新建会话，
                # 保证多轮压测不中断；主循环会接管返回的新 sid。
                problem, sid, via = _acquire_fresh(
                    client, mode, topic, difficulty, used_pool_ids)
                rec["acquired_via"] = f"{via}(recovered)"
                rec["context_compressed"] = False
                rec["recovered"] = True
                if verbose:
                    print(f"  ↺ 连续换题失败（{e}），已回退新建会话继续")
        else:
            problem, sid, via = _acquire_fresh(
                client, mode, topic, difficulty, used_pool_ids)
            rec["acquired_via"] = via
    except Exception as e:
        rec["error"] = f"出题失败: {e}"
        rec["acquire_sec"] = round(time.time() - t0, 2)
        rec["session_id"] = sid
        return rec
    rec["acquire_sec"] = round(time.time() - t0, 2)
    rec["session_id"] = sid
    rec["problem_id"] = problem.get("problem_id")
    rec["title"] = problem.get("title")
    rec["topic"] = problem.get("topic")
    rec["difficulty"] = problem.get("difficulty")
    if verbose:
        comp = "（上下文已压缩）" if rec["context_compressed"] else ""
        print(f"  📝 出题完成 [{rec['acquired_via']}]{comp} 《{rec['title']}》 "
              f"({rec['topic']}/{rec['difficulty']}) 用时 {rec['acquire_sec']}s")

    # ── 2. 连续运行 + 提交（带失败重试）──
    feedback = None
    for attempt in range(1, max_attempts + 1):
        rec["attempts"] = attempt
        # 2.1 生成做题代码
        tg = time.time()
        try:
            code = llm.generate(problem, feedback)
        except Exception as e:
            rec["error"] = f"LLM 生成失败(第{attempt}次): {e}"
            rec["gen_sec_total"] += round(time.time() - tg, 2)
            continue
        rec["gen_sec_total"] += round(time.time() - tg, 2)

        # 2.2 编辑轨迹：edit 事件（模拟用户在编辑器里写出这版代码）
        if emit_traces:
            change = ("初次生成解法（LeetCode 风格 class Solution）" if attempt == 1
                      else f"根据判题反馈修正（第{attempt}次）")
            client.emit_trace(sid, [{"type": "edit", "change": change, "ts": _now_ms()}],
                              problem_id=rec["problem_id"])

        # 2.3 运行（可见用例，便宜）
        tr = time.time()
        run_resp = None
        try:
            run_resp = client.run(sid, code)
            rec["run_sec_total"] += round(time.time() - tr, 2)
            if emit_traces:
                client.emit_trace(sid, [{"type": "run", "ts": _now_ms()}],
                                  problem_id=rec["problem_id"])
        except Exception as e:
            rec["error"] = f"运行失败(第{attempt}次): {e}"
            rec["run_sec_total"] += round(time.time() - tr, 2)
            feedback = f"运行请求异常：{e}"
            continue

        # 2.4 提交（全量判题；真实链路自动写画像 + flush 历史）
        ts = time.time()
        sub_resp = None
        try:
            sub_resp = client.submit(sid, code)
            rec["submit_sec_total"] += round(time.time() - ts, 2)
            if emit_traces:
                client.emit_trace(sid, [{"type": "submit", "ts": _now_ms()}],
                                  problem_id=rec["problem_id"])
        except Exception as e:
            rec["error"] = f"提交失败(第{attempt}次): {e}"
            rec["submit_sec_total"] += round(time.time() - ts, 2)
            feedback = f"提交请求异常：{e}"
            continue

        verdict = sub_resp.get("verdict")
        rec["verdict"] = verdict
        if verbose:
            all_passed = run_resp.get("all_passed")
            print(f"  🔁 第{attempt}次: run.all_passed={all_passed} submit.verdict={verdict}")

        if verdict == "AC":
            rec["ac"] = True
            rec["profile_updated"] = True  # 真实 submit 的 update_profile_node 已写画像
            break
        # 未 AC：拉取状态拿失败明细，构造反馈重生成
        try:
            state_resp = client.state(sid)
        except Exception:
            state_resp = None
        feedback = _build_feedback(problem, run_resp, state_resp, verdict)

    # ── 3. AC 后轨迹分析（按题隔离；连续模式再做过渡压缩双落点）──
    if rec["ac"]:
        if emit_traces:
            traj = _run_trajectory_analysis(
                client, sid, rec["problem_id"],
                do_summarize=(strategy == "continuous"),
                followup=followup,
            )
            rec["trajectory_analysis"] = traj.get("analysis")
            rec["trajectory_summary"] = traj.get("summary")
            rec["trajectory_followup"] = traj.get("followup")
            if traj.get("error"):
                rec["error"] = rec["error"] or traj["error"]
        if verbose:
            bits = ["轨迹分析已生成"]
            if rec.get("trajectory_summary"):
                bits.append("过渡压缩已落库(双落点)")
            if rec.get("trajectory_followup"):
                bits.append("多轮追问已答")
            print(f"  ✅ 第 {round_idx} 轮 AC（{rec['attempts']} 次尝试）"
                  f"{'；' + '、'.join(bits) if bits else ''}")
    elif verbose:
        print(f"  ❌ 第 {round_idx} 轮 未 AC（verdict={rec['verdict']}，{rec['attempts']} 次尝试）")
    return rec


# ───────────────────────────────────────────────────────────
#  报告
# ───────────────────────────────────────────────────────────
def _profile_attempts(profile: dict | None) -> int:
    """从 v2 画像中统计已记录的尝试题数（attempts 键的个数）。"""
    if not profile:
        return 0
    return len(profile.get("attempts") or {})


def _profile_v1_attempts(profile: dict | None) -> int:
    """从 v1 画像中取 attempts（int，已做题数）。"""
    if not profile:
        return 0
    a = profile.get("attempts", 0)
    return a if isinstance(a, int) else 0


def _profile_v1_proficiency(profile: dict | None):
    """从 v1 画像中取 proficiency（float，0~1）。"""
    if not profile:
        return None
    p = profile.get("proficiency")
    return float(p) if isinstance(p, (int, float)) else None


def build_report(records: list[dict], llm: SolverLLM, started_at: float,
                 args, profile_before: dict | None, profile_after: dict | None,
                 profile_v1_before: dict | None = None,
                 profile_v1_after: dict | None = None) -> dict:
    total = len(records)
    ac = sum(1 for r in records if r["ac"])
    err = sum(1 for r in records if r["error"])
    verdicts = {}
    for r in records:
        v = r["verdict"] or ("ERROR" if r["error"] else "NONE")
        verdicts[v] = verdicts.get(v, 0) + 1

    traj_done = sum(1 for r in records if r.get("trajectory_analysis") and not r["trajectory_analysis"].get("error"))
    traj_summary_done = sum(1 for r in records if r.get("trajectory_summary"))
    traj_followup_done = sum(1 for r in records if r.get("trajectory_followup"))
    prof_updated = sum(1 for r in records if r.get("profile_updated"))
    ctx_compressed = sum(1 for r in records if r.get("context_compressed"))

    def avg(key):
        vals = [r[key] for r in records if isinstance(r.get(key), (int, float))]
        return round(sum(vals) / len(vals), 2) if vals else 0.0

    wall = round(time.time() - started_at, 1)
    return {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "config": {
            "base_url": args.base_url,
            "mode": args.mode,
            "session_strategy": args.session_strategy,
            "rounds": args.rounds,
            "max_attempts": args.max_attempts,
            "topic": args.topic,
            "difficulty": args.difficulty,
            "emit_traces": args.emit_traces,
            "solver_model": llm.model,
        },
        "faithful_simulation": {
            "profile_updates": prof_updated,
            "context_compressions": ctx_compressed,
            "trajectory_analyses": traj_done,
            "trajectory_transition_summaries": traj_summary_done,
            "trajectory_followups": traj_followup_done,
            "profile_before_attempts": _profile_attempts(profile_before),
            "profile_after_attempts": _profile_attempts(profile_after),
            "profile_delta_attempts": _profile_attempts(profile_after) - _profile_attempts(profile_before),
            # v1 画像（proficiency/stability/attempts/common_errors），submit 同步更新
            "profile_v1_enabled": profile_v1_before is not None or profile_v1_after is not None,
            "profile_v1_before_attempts": _profile_v1_attempts(profile_v1_before),
            "profile_v1_after_attempts": _profile_v1_attempts(profile_v1_after),
            "profile_v1_delta_attempts": _profile_v1_attempts(profile_v1_after) - _profile_v1_attempts(profile_v1_before),
            "profile_v1_before_proficiency": _profile_v1_proficiency(profile_v1_before),
            "profile_v1_after_proficiency": _profile_v1_proficiency(profile_v1_after),
            "note": (
                "画像更新由真实 POST /submit 的 graph 链路自动完成，且存在两套独立画像："
                "v2 能力画像（update_profile_node→critic_node，5 维 prof/stab/forget/errors/attempts，"
                "默认观测、做前后对比）+ v1 画像（update_profile_on_result，proficiency/stability/attempts/common_errors，"
                "--profile-v1 开启后额外观测）。两者都只由 submit 触发；"
                "上下文压缩由连续模式的 POST /next-problem 触发（build_cross_problem_context + generate_summary，"
                "并在换题时读取上一题轨迹过渡摘要注入 context_summary）；"
                "轨迹分析由每轮 AC 后 POST /analyze（按题隔离、独立线程）触发，"
                "连续模式再 POST /analyze/summarize 做过渡压缩（双落点：可见卡 + 注入下一题导师上下文）。"
                "三者均通过对外 HTTP 接口驱动，未改动任何业务代码。"
                "注意：轨迹分析/过渡压缩纯展示、绝不回灌 profile/memory（commit f0668a6 标题具误导性）。"
            ),
        },
        "summary": {
            "total_rounds": total,
            "ac": ac,
            "ac_rate": round(ac / total * 100, 1) if total else 0.0,
            "errors": err,
            "verdict_distribution": verdicts,
            "avg_acquire_sec": avg("acquire_sec"),
            "avg_gen_sec": avg("gen_sec_total"),
            "avg_run_sec": avg("run_sec_total"),
            "avg_submit_sec": avg("submit_sec_total"),
            "total_wall_sec": wall,
            "total_prompt_tokens": llm.total_prompt_tokens,
            "total_completion_tokens": llm.total_completion_tokens,
        },
        "rounds": records,
    }


def print_summary(report: dict) -> None:
    s = report["summary"]
    fs = report.get("faithful_simulation", {})
    print()
    print("=" * 68)
    print("  📊 CodeTutor 连续做题自动化测试报告")
    print("=" * 68)
    print(f"  时间:        {report['timestamp']}")
    print(f"  目标服务:    {report['config']['base_url']}")
    print(f"  模式:        {report['config']['mode']}  "
          f"会话策略={report['config']['session_strategy']}")
    print(f"  做题模型:    {report['config']['solver_model']}")
    print(f"  轮数:        {s['total_rounds']}  最大提交/轮: {report['config']['max_attempts']}")
    print("-" * 68)
    print(f"  AC 率:       {s['ac']}/{s['total_rounds']} = {s['ac_rate']}%")
    print(f"  错误轮次:    {s['errors']}")
    print(f"  verdict 分布: {s['verdict_distribution']}")
    print("-" * 68)
    print(f"  🧬 忠实模拟真实流程:")
    print(f"     画像更新次数:   {fs.get('profile_updates', 0)}  "
          f"(画像尝试题数 {fs.get('profile_before_attempts', 0)} → {fs.get('profile_after_attempts', 0)}, "
          f"Δ={fs.get('profile_delta_attempts', 0)})")
    print(f"     上下文压缩次数: {fs.get('context_compressions', 0)}")
    if fs.get("profile_v1_enabled"):
        print(f"     画像(v1)更新:  尝试 {fs.get('profile_v1_before_attempts', 0)} → "
              f"{fs.get('profile_v1_after_attempts', 0)} (Δ={fs.get('profile_v1_delta_attempts', 0)}), "
              f"熟练度 {fs.get('profile_v1_before_proficiency')} → {fs.get('profile_v1_after_proficiency')}")
    print(f"     轨迹分析次数:   {fs.get('trajectory_analyses', 0)}"
          f"（过渡压缩 {fs.get('trajectory_transition_summaries', 0)} · 多轮追问 {fs.get('trajectory_followups', 0)}）")
    print("-" * 68)
    print(f"  平均出题:    {s['avg_acquire_sec']}s")
    print(f"  平均生成:    {s['avg_gen_sec']}s")
    print(f"  平均运行:    {s['avg_run_sec']}s")
    print(f"  平均提交:    {s['avg_submit_sec']}s")
    print(f"  总耗时:      {s['total_wall_sec']}s")
    print(f"  Token:       prompt={s['total_prompt_tokens']} "
          f"completion={s['total_completion_tokens']}")
    print("=" * 68)
    print("  明细:")
    for r in report["rounds"]:
        flag = "✅" if r["ac"] else ("⚠️" if r["error"] else "❌")
        comp = " 🗜️" if r.get("context_compressed") else ""
        recov = " ♻️" if r.get("recovered") else ""
        traj = " 🔍" if (r.get("trajectory_analysis") and not r["trajectory_analysis"].get("error")) else ""
        summ = " 🔗" if r.get("trajectory_summary") else ""
        print(f"    {flag} #{r['round']} 《{r['title']}》 "
              f"[{r['acquired_via']}]{comp}{recov}{traj}{summ} verdict={r['verdict']} "
              f"attempts={r['attempts']} t={r['acquire_sec']}s")
    print()
    print("  图例: 🗜️=上下文压缩(连续换题)  ♻️=连续换题失败回退新建会话  "
          "🔍=AC 后轨迹分析  🔗=过渡压缩(双落点)")


# ───────────────────────────────────────────────────────────
#  可选：服务未起时自动拉起
# ───────────────────────────────────────────────────────────
def maybe_auto_start(client: TutorClient, repo_root: Path, port: int) -> None:
    if client.health():
        return
    print("  🚀 服务未就绪，尝试自动拉起 (uv run uvicorn) ...")
    import subprocess
    cmd = [
        "uv", "run", "uvicorn", "src.code_tutor_agent.api.main:app",
        "--host", "127.0.0.1", "--port", str(port),
    ]
    proc = subprocess.Popen(cmd, cwd=str(repo_root),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 90
    while time.time() < deadline:
        if client.health():
            print("  ✅ 服务已就绪")
            return
        time.sleep(2)
    proc.terminate()
    raise RuntimeError("自动拉起服务失败，请手动启动后端后再运行")


# ───────────────────────────────────────────────────────────
#  main
# ───────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="CodeTutor Agent 连续做题自动化测试驱动（黑盒，零侵入；忠实模拟真实流程）")
    parser.add_argument("--base-url", default="http://localhost:8765",
                        help="后端服务地址（默认 http://localhost:8765）")
    parser.add_argument("--rounds", type=int, default=5, help="做题轮数（默认 5）")
    parser.add_argument("--mode", choices=["generator", "pool"], default="generator",
                        help="第一题出题模式：generator=真实出题链路；pool=复用题库（默认 generator）")
    parser.add_argument("--session-strategy", choices=["continuous", "fresh"], default="continuous",
                        help="会话策略：continuous=多轮复用同一会话(触发上下文压缩，忠实模拟)；"
                             "fresh=每轮新建独立会话(不压缩上下文)")
    parser.add_argument("--topic", default=None, help="限定主题（仅 generator 出题生效；连续模式每轮沿用）")
    parser.add_argument("--difficulty", default=None,
                        choices=["easy", "medium", "hard"], help="限定难度（仅 generator 出题生效）")
    parser.add_argument("--max-attempts", type=int, default=2,
                        help="单轮最大「运行+提交」尝试次数（默认 2，WA 时回灌重生成）")
    parser.add_argument("--solver-model", default=os.getenv("LLM_MODEL"),
                        help="做题用模型（默认复用 LLM_MODEL）")
    parser.add_argument("--solver-base-url", default=os.getenv("LLM_BASE_URL"),
                        help="做题用网关（默认复用 LLM_BASE_URL）")
    parser.add_argument("--solver-api-key", default=os.getenv("LLM_API_KEY"),
                        help="做题用 API Key（默认复用 LLM_API_KEY）")
    parser.add_argument("--emit-traces", dest="emit_traces", action="store_true", default=True,
                        help="采集编辑轨迹事件并发往 /edit-trace（默认开；轨迹分析需要）")
    parser.add_argument("--no-traces", dest="emit_traces", action="store_false",
                        help="关闭编辑轨迹采集（同时关闭 AC 后轨迹分析）")
    parser.add_argument("--trajectory-followup", default=None,
                        help="可选：AC 复盘后再发一条多轮追问（如『为什么我卡了这么久？』），"
                             "演示新架构的「独立线程多轮追问」能力。默认不追问以省 LLM。")
    parser.add_argument("--profile-snapshot", dest="profile_snapshot", action="store_true", default=True,
                        help="前后抓取 /admin/profile/v2 对比画像更新（默认开；失败则忽略）")
    parser.add_argument("--no-profile-snapshot", dest="profile_snapshot", action="store_false",
                        help="不做画像前后快照")
    parser.add_argument("--profile-v1", dest="profile_v1", action="store_true", default=False,
                        help="额外抓取 /admin/profile（v1 画像 proficiency/stability/attempts/common_errors）"
                             "做前后对比；submit 会同时更新 v1 与 v2 两套画像（默认只观测 v2）")
    parser.add_argument("--report", default="", help="报告 JSON 落盘路径")
    parser.add_argument("--auto-start", action="store_true",
                        help="服务未就绪时自动拉起后端（uv run uvicorn）")
    parser.add_argument("--validate-only", action="store_true",
                        help="仅校验连通性 + 出题契约，不出题求解")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    args = parser.parse_args()

    if not args.solver_api_key:
        print("❌ 未检测到 LLM_API_KEY（请检查 .env）")
        sys.exit(1)

    client = TutorClient(args.base_url)
    repo_root = Path(__file__).resolve().parent.parent

    if args.auto_start:
        maybe_auto_start(client, repo_root, 8765)

    if not client.health():
        print(f"❌ 后端服务不可达：{args.base_url}，请先启动后端（或加 --auto-start）")
        sys.exit(1)
    print(f"✅ 后端健康：{args.base_url}")

    if args.validate_only:
        print("🔍 校验出题契约（generator 全链路：建会话→对话→出题）...")
        sid = client.create_session(args.topic, args.difficulty)
        client.wait_for_dialog(sid)
        client.complete_dialog(sid, args.topic, args.difficulty)
        try:
            problem = client.wait_ready(sid)
        except Exception as e:
            print(f"❌ 出题失败：{e}")
            sys.exit(1)
        keys = sorted(problem.keys())
        print(f"✅ 出题成功：《{problem.get('title')}》")
        print(f"   problem 字段: {keys}")
        print(f"   visible_test_cases 条数: {len(problem.get('visible_test_cases') or [])}")
        print(f"   starter_code 长度: {len(problem.get('starter_code') or '')}")
        # 顺带校验轨迹分析端点可用（新架构：按题隔离 + 过渡压缩）
        try:
            pid = problem.get("problem_id")
            client.emit_trace(sid, [{"type": "edit", "change": "校验", "ts": _now_ms()}],
                              problem_id=pid)
            an = client.analyze(sid, pid)
            print("✅ 轨迹分析端点可用（POST /session/{id}/analyze，按题隔离）")
            ga = client.get_analysis(sid, pid)
            print("✅ 轨迹分析读取可用（GET /session/{id}/analysis?problem_id=）")
            if pid is not None:
                sm = client.summarize(sid, pid, transition_action="continue")
                print("✅ 轨迹过渡压缩可用（POST /session/{id}/analyze/summarize，双落点）")
        except requests.RequestException as e:
            print(f"   ⚠️ 轨迹分析端点校验跳过：{e}")
        print("✅ 契约校验通过。")
        return

    llm = SolverLLM(
        model=args.solver_model or "deepseek-v4-flash",
        base_url=args.solver_base_url or "https://api.deepseek.com",
        api_key=args.solver_api_key,
    )

    profile_before = None
    if args.profile_snapshot:
        profile_before = client.get_profile_v2()
        if profile_before is None:
            print("  ⚠️ 画像快照抓取失败（/admin/profile/v2 不可达），跳过前后对比")
        elif args.verbose:
            print(f"  🧬 画像起始尝试题数: {_profile_attempts(profile_before)}")

    profile_v1_before = client.get_profile_v1() if args.profile_v1 else None
    if profile_v1_before is not None and args.verbose:
        print(f"  🧬 v1 画像起始: attempts={_profile_v1_attempts(profile_v1_before)}, "
              f"proficiency={_profile_v1_proficiency(profile_v1_before)}")

    print()
    strategy_label = "连续单会话(含上下文压缩)" if args.session_strategy == "continuous" else "每轮新建会话"
    print(f"🏁 开始连续做题：{args.rounds} 轮，模式={args.mode}，策略={strategy_label}，"
          f"每轮最多 {args.max_attempts} 次尝试"
          f"{'（含轨迹分析）' if args.emit_traces else '（无轨迹分析）'}")
    print()

    started_at = time.time()
    records = []
    used_pool_ids: set[int] = set()
    current_sid: str | None = None
    for i in range(1, args.rounds + 1):
        print(f"▶ 第 {i}/{args.rounds} 轮 ...")
        sid_arg = current_sid if args.session_strategy == "continuous" else None
        rec = run_one_round(
            client=client, llm=llm, round_idx=i, mode=args.mode,
            topic=args.topic, difficulty=args.difficulty,
            max_attempts=args.max_attempts, used_pool_ids=used_pool_ids,
            verbose=args.verbose, strategy=args.session_strategy,
            sid=sid_arg, emit_traces=args.emit_traces,
            followup=args.trajectory_followup,
        )
        records.append(rec)
        # 连续模式：后续轮复用同一会话
        if args.session_strategy == "continuous":
            current_sid = rec["session_id"]
        if rec["error"] and args.verbose:
            print(f"  ⚠️ 错误: {rec['error']}")

    profile_after = None
    if args.profile_snapshot:
        profile_after = client.get_profile_v2()

    profile_v1_after = client.get_profile_v1() if args.profile_v1 else None

    report = build_report(records, llm, started_at, args, profile_before, profile_after,
                          profile_v1_before, profile_v1_after)
    print_summary(report)

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"📄 报告已保存：{args.report}")


if __name__ == "__main__":
    main()
