"""批量生成各 topic 的出题维度数据（写入 data/problem-dimension/{topic}.json）。

设计要点（按用户 2026-08-27 要求）：
- 调用独立的维度生成模型。默认 glm-5.2（.env 的 LLM_*_DIM）；
  --model gmi 可切到 GMI（GMI_API_KEY / GMI_BASE_URL / GMI_OPENAI_MODEL）；
  --model auto 先 glm，遇 429/异常退避后自动换 GMI。
- **不解析、不规整**：模型原始返回（raw）直接落盘，即使 JSON 不完整也先存，
  后续由单独脚本统一补格式。故本脚本只负责「调用 + 存原文」。
- topic 列表默认取系统 known_topics（agent_dialog.py 权威枚举）。
- 已有维度文件的 topic 默认跳过（补全语义）；--force 可覆盖。
- 请求间隔默认 60s（glm 流量控制）；429 自动退避重试（--retries）。

运行：
  env -u PYTHONPATH .venv/Scripts/python.exe scripts/generate_dimensions.py
  env -u PYTHONPATH .venv/Scripts/python.exe scripts/generate_dimensions.py --force
  env -u PYTHONPATH .venv/Scripts/python.exe scripts/generate_dimensions.py --model gmi
  env -u PYTHONPATH .venv/Scripts/python.exe scripts/generate_dimensions.py --topics 栈 队列 图
  env -u PYTHONPATH .venv/Scripts/python.exe scripts/generate_dimensions.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:
    pass

from langchain.chat_models import init_chat_model  # noqa: E402

DIM_DIR = os.path.join(ROOT, "data", "problem-dimension")

# 系统权威 topic 枚举（与 agent_dialog.py known_topics 保持一致）
DEFAULT_TOPICS = [
    "动态规划", "数组", "链表", "二叉树", "字符串", "回溯", "贪心",
    "双指针", "滑动窗口", "二分查找", "栈", "队列", "哈希表", "排序",
    "递归", "前缀和", "位运算", "图", "堆", "并查集",
]

# 维度生成 prompt（用户给定模板，{topic} 占位；原文示例「链表」已泛化为 {topic}）
DIM_PROMPT = """# 角色设定
你是一名拥有深厚计算机科学教育背景的 LeetCode 资深出题师。你精通{topic}的所有主流题型、冷门变体以及近年来出现的新颖考察角度。你的核心能力在于识别考点，并构建能够真正检验候选人算法思维而非记忆力的题目方向。

# 背景与目标
鉴于候选人可能已经通过背诵经典题库来应对面试，你的任务是为{topic}这一主题，挖掘出除经典模板题之外的多种出题方向。你需要从多个维度全面梳理可能的考察点，生成一份详尽的方向清单，供我筛选后进入具体的出题阶段。

# 任务要求
1. **多维分析**：请从数据结构特性、算法策略、边界条件、优化技巧等多个维度进行拆解。
2. **少出现经典**：聚焦于变种、综合应用或新颖视角。
3. **详细定义**：每个维度需要包含清晰的名称、考察核心、典型陷阱或难点，以及可能的变体方向。
4. **发散思维**  结合多个场景考虑情况，覆盖这个主题的各个题目类型，但不要偏题

# 输出格式
请以纯 JSON 数组格式输出，严禁包含任何 Markdown 代码块标记（如 ```json）、前言或后记。JSON 结构如下：
[
  {
    "dimension_name": "维度名称",
    "description": "该维度的详细解释",
    "example_directions": ["子方向1", "子方向2", "子方向3... 子方向10"],
  }
]

# 当前输入参数
- 主题：{topic}

请直接输出符合上述结构的 JSON 数据。"""


def _build_llm(profile: str):
    """按 profile 取配置构造 LLM。profile: 'dim' | 'gmi'。"""
    if profile == "gmi":
        model = os.getenv("GMI_OPENAI_MODEL")
        base_url = os.getenv("GMI_BASE_URL")
        api_key = os.getenv("GMI_API_KEY")
        tag = "GMI"
    else:
        model = os.getenv("LLM_MODEL_DIM")
        base_url = os.getenv("LLM_BASE_URL_DIM")
        api_key = os.getenv("LLM_API_KEY_DIM")
        tag = "DIM(glm)"
    if not (model and base_url and api_key):
        raise RuntimeError(f"{tag} 配置缺失：model/base_url/api_key 不可为空")
    llm = init_chat_model(
        model,
        model_provider="openai",
        base_url=base_url,
        api_key=api_key,
        temperature=0.8,
        max_tokens=8192,
    )
    return llm, tag


def _is_rate_limit(exc) -> bool:
    return "429" in str(getattr(exc, "status_code", "")) or \
        "RateLimitError" in type(exc).__name__ or \
        "quota" in str(exc).lower() or "rate limit" in str(exc).lower()


def call_with_retry(prompt: str, model_arg: str, interval: float,
                    retries: int):
    """调用 LLM，带 429 退避重试；auto 模式 glm 失败换 gmi。"""
    profiles = (["dim", "gmi"] if model_arg == "auto"
                else [model_arg])
    last_err = None
    for attempt in range(retries):
        for profile in profiles:
            try:
                llm, tag = _build_llm(profile)
                resp = llm.invoke(prompt)
                raw = resp.content if hasattr(resp, "content") else str(resp)
                return raw, tag
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if _is_rate_limit(exc):
                    wait = interval * (attempt + 1)
                    print(f"    ⏳ {type(exc).__name__}（限流/配额），"
                          f"退避 {wait:.0f}s 后重试 "
                          f"[{attempt+1}/{retries}]", flush=True)
                    time.sleep(wait)
                else:
                    print(f"    ⚠️ {tag} 调用异常: {exc}", flush=True)
                    time.sleep(interval)
    raise last_err or RuntimeError("未知错误")


def main():
    ap = argparse.ArgumentParser(description="批量生成各 topic 出题维度数据")
    ap.add_argument("--topics", nargs="*", default=None,
                    help="指定 topic 列表；缺省用 DEFAULT_TOPICS")
    ap.add_argument("--model", choices=["dim", "gmi", "auto"], default="auto",
                    help="dim=glm-5.2 / gmi=GMI / auto=先glm失败换gmi")
    ap.add_argument("--interval", type=float, default=60.0,
                    help="每个 topic 请求间隔秒数，默认 60")
    ap.add_argument("--retries", type=int, default=5,
                    help="单 topic 最大重试次数（含限流退避），默认 5")
    ap.add_argument("--force", action="store_true",
                    help="覆盖已存在的维度文件；默认跳过已存在")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将生成的 topic，不真正调用")
    args = ap.parse_args()

    topics = args.topics or DEFAULT_TOPICS
    os.makedirs(DIM_DIR, exist_ok=True)

    todo = []
    for t in topics:
        fp = os.path.join(DIM_DIR, f"{t}.json")
        if os.path.exists(fp) and not args.force:
            print(f"  [skip] {t} 已存在维度文件（--force 可覆盖）", flush=True)
            continue
        todo.append(t)

    print(f"模型策略={args.model} 计划生成 {len(todo)} 个 topic "
          f"（间隔 {args.interval}s，重试 {args.retries}）：{todo}", flush=True)
    if args.dry_run:
        return
    if not todo:
        print("无待生成项，退出。", flush=True)
        return

    ok, fail = 0, 0
    for i, t in enumerate(todo):
        if i > 0:
            time.sleep(args.interval)
        print(f"\n=== [{i+1}/{len(todo)}] 生成 {t} 维度 ===", flush=True)
        prompt = DIM_PROMPT.replace("{topic}", t)
        try:
            raw, tag = call_with_retry(prompt, args.model, args.interval,
                                       args.retries)
            out = os.path.join(DIM_DIR, f"{t}.json")
            # 原样落盘（不解析、不规整；不完整也先存，后续统一补格式）
            with open(out, "w", encoding="utf-8") as f:
                f.write(raw)
            print(f"  ✅ {t}: 已存原始返回（{tag}，{len(raw)} 字符）-> {out}",
                  flush=True)
            ok += 1
        except Exception as exc:
            print(f"  ❌ {t} 最终失败: {exc}", flush=True)
            fail += 1

    print(f"\n完成：成功 {ok}，失败 {fail}。", flush=True)


if __name__ == "__main__":
    main()
