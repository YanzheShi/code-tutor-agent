"""轨迹分析的结构化输出 schema（默认 json_schema 路径，thinking 模式安全，严禁 function_calling）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class WeaknessTag(BaseModel):
    """复盘暴露的一个薄弱点 / 思维特征（自由文本，不绑定画像维度）。"""

    tag: str = Field(description="薄弱点描述（自由文本，例如：边界处理考虑不全 / 状态定义不清）")
    evidence: str = Field(default="", description="来自轨迹或代码的一条证据（一句话）")
    severity: float = Field(default=0.5, ge=0.0, le=1.0, description="严重度 0~1，越接近 1 越值得优先补")
    trigger: str = Field(
        default="self",
        description="这处是独立改对还是被提示改对：self | hint | boundary_reminder | correction_assisted",
    )
    hint_assisted: bool = Field(default=False, description="是否依赖导师提示才改对")
    hints_before_fix: int = Field(default=0, description="改对前收到几次相关提示")


class InterviewTip(BaseModel):
    """给面试备考的一条建议。"""

    point: str = Field(description="一条可执行的面试备考建议")
    reason: str = Field(default="", description="为什么给这条建议（结合本次做题轨迹）")


class ChangeStep(BaseModel):
    """变化路径的一步。"""

    step: int = Field(description="步骤序号")
    action: str = Field(description="这一步做了什么（如：写了循环又删掉 / 卡壳很久）")
    trigger: str = Field(
        default="self",
        description="self | hint | boundary_reminder | correction_assisted",
    )
    note: str = Field(default="", description="补充说明")


class Autonomy(BaseModel):
    """面试就绪度信号：独立改对能力。"""

    self_fix_rate: float = Field(default=0.0, ge=0.0, le=1.0, description="独立改对率 0~1")
    hint_dependent_weaknesses: list[str] = Field(
        default_factory=list, description="依赖提示才过的薄弱点列表"
    )


class AnalysisResult(BaseModel):
    """一次做题轨迹的独立复盘结果（纯展示，不回灌画像）。"""

    summary: str = Field(default="", description="一句话总评")
    change_path: list[ChangeStep] = Field(default_factory=list, description="代码一步步写出来的变化路径")
    thinking_process: str = Field(default="", description="推断用户的解题思维过程")
    weakness_tags: list[WeaknessTag] = Field(default_factory=list)
    interview_tips: list[InterviewTip] = Field(default_factory=list)
    autonomy: Autonomy = Field(default_factory=Autonomy)


class TraceSummary(BaseModel):
    """过渡压缩摘要（双落点：可见卡 + 历史回看）。"""

    summary_text: str = Field(default="", description="≤500 字叙述")
    bullets: list[str] = Field(default_factory=list, description="≤10 条要点")
    autonomy: Autonomy = Field(default_factory=Autonomy)
    hint_dependence: list[str] = Field(default_factory=list, description="hint 依赖要点")
