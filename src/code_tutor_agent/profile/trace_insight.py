"""独立的「做题轨迹分析」（与用户能力画像解耦）。

与 profile/edit_trace_analyzer.py 的区别：
- 那里是「错误模式画像」的 feeder，输出被强制约束到 6 维 slug，且会回灌 DBProfile.error_modes；
- 这里是**纯展示用**的独立复盘：tag 是自由文本，可描述任何薄弱点 / 思维特征，
  **绝不**写入 profile / memory，仅供用户 AC 后手动点开自我复盘。

数据流：读 edit_traces 全量事件 → 预处理成紧凑时间线 → 注入本题上下文 →
LLM 一次性产出结构化复盘（change_path / thinking_process / weakness_tags / interview_tips / summary）。
非致命：任何异常都返回带说明文案的空结果，不影响主流程。
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field

from code_tutor_agent.config import get_llm
from code_tutor_agent.db.database import get_edit_trace
from code_tutor_agent.profile.edit_trace_analyzer import build_trace_timeline

logger = logging.getLogger(__name__)


# ── LLM 输出 schema（function_calling，自由文本，不约束 6 维）──


class TraceWeaknessTag(BaseModel):
    """复盘暴露的一个薄弱点 / 思维特征（自由文本，不绑定画像维度）。"""
    tag: str = Field(description="薄弱点描述（自由文本，例如：边界处理考虑不全 / 先写暴力后优化但状态没想清）")
    evidence: str = Field(default="", description="来自轨迹或代码的一条证据（一句话）")
    severity: float = Field(default=0.5, ge=0.0, le=1.0, description="严重度 0~1，越接近 1 越值得优先补")


class TraceInterviewTip(BaseModel):
    """给面试备考的一条建议。"""
    point: str = Field(description="一条可执行的面试备考建议")
    reason: str = Field(default="", description="为什么给这条建议（结合本次做题轨迹）")


class TraceAnalysisResult(BaseModel):
    """一次做题轨迹的独立复盘结果（纯展示，不回灌画像）。"""
    summary: str = Field(default="", description="一句话总评")
    change_path: str = Field(default="", description="代码是怎么一步步写出来的（变化路径叙述）")
    thinking_process: str = Field(default="", description="推断用户的解题思维过程")
    weakness_tags: list[TraceWeaknessTag] = Field(default_factory=list)
    interview_tips: list[TraceInterviewTip] = Field(default_factory=list)


_PROMPT = """你是一个编程教练，专门分析一次算法题「做题过程」的编辑轨迹，帮助用户复盘。
注意：本次分析是**独立**的、仅供用户自我复盘参考，**不要**写成能力评分，也**不要**关联任何历史画像。

## 本题信息
- 知识点: {topic}
- 题目描述(节选): {description}

## 最终提交的代码
```python
{final_code}
```

## 编辑轨迹时间线（反映"怎么做出来的":卡壳 / 反复修改 / 试错 / 提交前自查）
{timeline}

## 任务
基于轨迹与最终代码，产出以下结构化复盘：
1. change_path：用 2-4 句话叙述代码是怎么一步步写出来的（哪里反复改、哪里卡壳很久、最后如何收口）。
2. thinking_process：推断用户的解题思维过程（思路是否清晰、是否走了弯路、是否先写暴力再优化、是否提交前自查）。
3. weakness_tags：列出暴露出的薄弱点（自由文本 tag，如「边界处理考虑不全」「状态定义不清」等），每条给 evidence（来自轨迹或代码）与 severity(0~1)。若无明显薄弱点则返回空列表。
4. interview_tips：给 2-4 条面试备考建议（point + reason，结合本次轨迹）。
5. summary：一句话总评。

只输出结构化结果，不要额外解释。
"""


def analyze_trace_standalone(
    session_id: str,
    *,
    topic: str = "",
    description: str = "",
    final_code: str = "",
) -> TraceAnalysisResult:
    """分析某会话的编辑轨迹，产出**独立**的做题复盘（不写 profile，失败返回空结果）。"""
    try:
        events = get_edit_trace(session_id)
    except Exception as exc:
        logger.warning("analyze_trace_standalone: read trace failed for %s: %s", session_id, exc)
        events = []

    if not events:
        logger.info("analyze_trace_standalone: no events for %s, return empty", session_id)
        return TraceAnalysisResult(
            summary="这次做题没有采集到编辑轨迹，无法生成复盘。",
            change_path="（无轨迹数据）",
        )

    timeline = build_trace_timeline(events)
    prompt = _PROMPT.format(
        topic=topic or "(未知)",
        description=(description or "")[:600],
        final_code=(final_code or "")[:4000],
        timeline=timeline,
    )

    try:
        llm = get_llm(purpose="edit-trace")
        # 默认 method（json_schema）：sensenova 网关思考模式下拒绝 function_calling 的
        # 强制 tool_choice（400），默认 json_schema 路径可用（与 judge/problem 等一致）。
        structured = llm.with_structured_output(TraceAnalysisResult)
        result = structured.invoke(prompt)
        if not isinstance(result, TraceAnalysisResult):
            result = TraceAnalysisResult(**(result if isinstance(result, dict) else {}))
        logger.info(
            "analyze_trace_standalone(%s) → %d weakness, %d tips",
            session_id, len(result.weakness_tags), len(result.interview_tips),
        )
        return result
    except Exception as exc:
        logger.error("analyze_trace_standalone: LLM failed for %s: %s", session_id, exc)
        return TraceAnalysisResult(
            summary="轨迹分析暂时不可用（模型调用失败），稍后再试一次吧。",
            change_path="",
        )
