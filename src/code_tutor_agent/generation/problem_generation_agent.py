"""出题子 Agent 编排 — 确定性决策树（docs/generation-subagent-design.md §4/§5）。

决策树（对应 Excalidraw 图）：

    D1{lc url 空?}
     ├─ 否 → import: fetch → to_lc_dict → 补双解(可选) → 成功 ⇒ 后台补用例 → SAVE
     │       失败 ⇒ 显式兜底链（绝不静默生成原创题）
     └─ 是 → llm_gen: 生成(双解+示例) → verify(结构/编译/无CoT)
               ├─ 通过 ⇒ 后台补用例 → SAVE
               └─ 失败 ⇒ 重试 < MAX ？ 重试 ；否则
                         pull: LeetCode 按 topic+difficulty 拉题
                           ├─ 成功 ⇒ 转 import 通道
                           └─ 失败 ⇒ history: DB 未 AC 题
                                      ├─ 命中 ⇒ SAVE
                                      └─ 未命中 ⇒ static: 静态题库
                                                  ├─ 命中 ⇒ SAVE
                                                  └─ 全失败 ⇒ error

决策树是确定性控制流（无环、无并行），用普通 Python 流程控制实现；
任务拆解到函数，不建状态机 / LangGraph 子图（设计 §1「不采用」）。
"""

from __future__ import annotations

import logging
from typing import Callable

from code_tutor_agent.agents.agent_problem import ProblemChannel
from code_tutor_agent.generation.gateways import (
    LeetCodeGateway,
    LlmGateway,
    SandboxGateway,
    StoreGateway,
)
from code_tutor_agent.generation.gateways.leetcode import slug_from_url
from code_tutor_agent.generation.state import (
    GenerationContext,
    GenerationResult,
    GenEvent,
    NullSink,
    ProblemDraft,
    ProgressSink,
)
from code_tutor_agent.generation.verifier import CodeVerifier

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

# 参考解在这些状态下说明 input 本身有问题（或参考解崩了），该用例应丢弃
_DROP_STATUSES = {"Runtime Error", "TLE", "Judge Error"}


class ProblemGenerationAgent:
    """出题子 Agent：一处入口 + 一条数据化降级链（设计 §2）。

    用法::

        result = ProblemGenerationAgent().run(ctx)
        if result.ok:
            print(result.channel, result.problem_id)   # llm / leetcode_import / ...
    """

    def __init__(
        self,
        leetcode: LeetCodeGateway | None = None,
        llm: LlmGateway | None = None,
        store: StoreGateway | None = None,
        sandbox: SandboxGateway | None = None,
        verifier: CodeVerifier | None = None,
    ):
        self.leetcode = leetcode or LeetCodeGateway()
        self.llm = llm or LlmGateway()
        self.store = store or StoreGateway()
        self.sandbox = sandbox or SandboxGateway()
        self.verifier = verifier or CodeVerifier()

    def run(self, ctx: GenerationContext, sink: ProgressSink | None = None) -> GenerationResult:
        """执行决策树，返回 GenerationResult（纯数据）。"""
        sink = sink or NullSink()
        draft: ProblemDraft | None = None
        attempted_chain: list[str] = []

        # ── 通道 A：LeetCode 导入 ──
        if ctx.lc_url:
            sink.event(GenEvent("progress", "📥 使用 LeetCode 题目…"))
            draft = self._import_from_leetcode(ctx, sink)
            if draft is None:
                # 导入失败 → 直进显式兜底链（设计 §4 D1：绝不静默生成原创题）
                sink.event(GenEvent("warning", "LeetCode 导入失败，转入兜底链（不静默生成原创题）"))
                logger.warning("leetcode import failed → fallback chain")
            elif ctx.options.dual_solution:
                # 生成暴力解法
                draft = self._ensure_dual(draft, sink)

        # ── 通道 B：LLM 生成 + 校验 + 重试（仅未贴 LeetCode 时）──
        if draft is None and not ctx.lc_url:
            sink.event(GenEvent("progress", "正在调用大模型生成题目…"))
            # 这里的重试有两层：
            # 外层重试： 针对 LLM 出题，结构没有问题，但是题目不能自下，就是给的是写法，不能跑通自身的示例输入输出。
            # 内层重试： LlmGateway.generate_problem 里面重试一次，针对LLM 出的题目结构不对，比如说缺少字段，题解代码有问题等
            # 对于网络问题，langchain会自己重试，默认再重试2次
            for _attempt in range(ctx.options.max_retries or MAX_RETRIES):
                draft = self._llm_generate(ctx, sink)
                if draft and self.verifier.verify(draft)[0]:
                    break
                draft = None

        # 判断出题的通道
        channel = self._channel_of(draft)

        # ── 降级链：PULL → HISTORY → STATIC（数据化，每步独立可测）──
        # 按照优先级执行，并且记录对应chanel
        if draft is None:
            for name, step in _FALLBACK_CHAIN:
                attempted_chain.append(name)
                try:
                    found, step_channel = step(self, ctx, sink)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("fallback %s failed: %s", name, exc)
                    continue
                if found is not None:
                    draft, channel = found, step_channel
                    sink.event(GenEvent("progress", f"降级到 {name} 成功"))
                    break

        if draft is None:
            sink.event(GenEvent("error", "出题失败：所有通道均不可用"))
            return GenerationResult(
                ok=False,
                channel=None,
                error="出题失败：所有通道均不可用",
                fallback_chain=attempted_chain,
            )

        # ── 先落库拿 id，用例后台补（不挡首屏）──
        try:
            pid = self.store.save(draft)
        except Exception as exc:
            logger.error("持久化失败: %s", exc)
            return GenerationResult(
                ok=False,
                channel=channel,
                error=f"题目已生成但落库失败: {exc}",
                draft=draft,
                fallback_chain=attempted_chain,
            )
        # 完整测试用例由 API 层在 graph.invoke 返回后统一调度 build_suite，
        # 不在包内自调度（线程内无事件循环会静默跳过，且会与 API 层双跑）。
        sink.event(GenEvent("progress", "✅ 题目已就绪！"))
        return GenerationResult(
            ok=True,
            channel=channel,
            problem_id=pid,
            draft=draft,
            test_cases_ready=False,  # 后台补全契约
            fallback_chain=attempted_chain,
        )

    # ── 通道 A：LeetCode 导入 ──
    def _import_from_leetcode(
        self, ctx: GenerationContext, sink: ProgressSink,
    ) -> ProblemDraft | None:
        data = self._lc_data(ctx)
        if not data:
            return None
        draft = self._lc_dict_to_draft(data, ctx)
        if draft is None:
            return None
        # 有 URL 时回填真实 slug（dict 导入无 slug，靠 imported 标记推导通道）
        if ctx.lc_url:
            draft.source_slug = slug_from_url(ctx.lc_url) or ""
        # 可选：补暴力解（对拍）已在外层 dual_solution 处理；此处仅同步补最优解
        if not draft.optimal_solution:
            code = self.llm.generate_optimal(
                draft.title,
                draft.description,
                draft.difficulty,
                draft.starter_code,
                draft.function_signature,
            )
            if code is not None:
                draft.optimal_solution = code
                sink.event(GenEvent("progress", "🤖 最优解代码已生成"))
            else:
                sink.event(GenEvent("warning", "最优解代码生成失败（不影响做题）"))
        # dict 导入无 slug，显式打上导入标记，保证 from_leetcode 落库判定正确
        draft.imported = True
        return draft

    def _lc_data(self, ctx: GenerationContext) -> dict | None:
        """按 URL 抓取 LeetCode 题目（解析统一收口到本题，不再接受预解析 dict）。"""
        slug = slug_from_url(ctx.lc_url or "")
        if not slug:
            logger.warning("lc_url 无法解析 slug: %r", ctx.lc_url)
            return None
        try:
            return self.leetcode.fetch(slug)
        except Exception as exc:
            logger.warning("LeetCode fetch 失败 %s: %s", slug, exc)
            return None

    def _lc_dict_to_draft(self, data: dict, ctx: GenerationContext) -> ProblemDraft | None:
        """to_lc_dict 产物 → ProblemDraft（镜像 _generate_from_leetcode 的落库字段）。"""
        title = data.get("title") or ""
        description = data.get("description") or ""
        difficulty = (data.get("difficulty") or ctx.difficulty or "medium").lower()
        starter_code = data.get("starter_code") or ""
        examples = list(data.get("examples") or [])

        # 树/图/链表结构体定义注入（先于签名提取，镜像现状）
        prologue = self.sandbox.struct_prologue(ctx.topic, description, starter_code)
        if prologue and not starter_code.startswith(prologue.strip()[:20]):
            starter_code = prologue + starter_code

        func_sig = data.get("function_signature") or ""
        if not func_sig:
            func_sig = self.leetcode.extract_signature(starter_code)

        # 可见用例：优先 parser 已解析的 parsed_test_cases，缺失时回退示例重解析
        test_cases = list(data.get("parsed_test_cases") or [])
        if not test_cases:
            test_cases = self.leetcode.parse_examples(examples, starter_code)
        if not title or not description or not starter_code:
            logger.warning("LeetCode 数据不完整（title/desc/starter），放弃导入")
            return None

        return ProblemDraft(
            topic=ctx.topic,
            difficulty=difficulty,
            title=title,
            description=description,
            description_html=data.get("description_html", "") or "",
            starter_code=starter_code,
            examples=examples,
            constraints=list(data.get("constraints") or []),
            tags=list(data.get("tags") or []),
            function_signature=func_sig,
            test_cases=test_cases,
        )

    def _ensure_dual(self, draft: ProblemDraft, sink: ProgressSink) -> ProblemDraft:
        """（可选）为导入题补暴力解，形成双参考解对拍。"""
        if draft.brute_solution:
            return draft
        code = self.llm.generate_dual(draft.description, draft.starter_code, mode="brute")
        if code and self.sandbox.compile(code):
            draft.brute_solution = code
            sink.event(GenEvent("progress", "🧪 已补充暴力解（双参考解对拍）"))
        else:
            sink.event(GenEvent("warning", "暴力解补充失败（保持单解验证）"))
        return draft

    # ── 通道 B：LLM 生成 ──
    def _llm_generate(self, ctx: GenerationContext, sink: ProgressSink) -> ProblemDraft | None:
        draft = self.llm.generate_problem(ctx.topic, ctx.difficulty)
        if draft is None:
            return None
        sink.event(GenEvent("progress", "🧪 正在解析示例测试用例…"))
        sample = self._build_sample_tests(draft)
        if sample is None:
            sink.event(GenEvent("warning", "示例用例校验失败，重试或走降级链"))
            return None
        draft.test_cases = sample
        return draft

    def _build_sample_tests(self, draft: ProblemDraft) -> list[dict] | None:
        """解析示例 → 参考解自验证回填 expected_output；任一失败返回 None。"""
        starter = draft.starter_code or draft.function_signature
        tcs = self.leetcode.parse_examples(draft.examples, starter)
        if not tcs:
            logger.warning("示例解析失败")
            return None
        reference = draft.optimal_solution or draft.brute_solution
        if not reference:
            logger.warning("无参考解，无法自验证示例")
            return None
        for tc in tcs:
            results = self.sandbox.run_solution(
                reference, [tc], function_signature=draft.function_signature,
            )
            if not results:
                logger.warning("参考解自验证失败: %s 无结果", tc.get("input_args"))
                return None
            r = results[0]
            if r.status in _DROP_STATUSES or not (r.detail or ""):
                logger.warning("参考解自验证失败: %s on %s", r.status, tc.get("input_args"))
                return None
            tc["expected_output"] = r.detail
        return tcs

    # ── 降级链各步 ──
    def _pull_from_leetcode(
        self, ctx: GenerationContext, sink: ProgressSink,
    ) -> tuple[ProblemDraft | None, str]:
        """PULL：LLM 失败后按主题+难度拉取 LeetCode 题。"""
        sink.event(GenEvent("progress", "🔄 正在从 LeetCode 按主题拉题…"))
        slugs = self.leetcode.list(ctx.topic, ctx.difficulty, limit=10)
        for slug in slugs[:5]:
            try:
                data = self.leetcode.fetch(slug)
            except Exception as exc:  # noqa: BLE001
                logger.warning("PULL fetch %s failed: %s", slug, exc)
                continue
            draft = self._lc_dict_to_draft(data, ctx)
            if draft is None:
                continue
            draft.source_slug = slug
            if not draft.optimal_solution:
                code = self.llm.generate_optimal(
                    draft.title, draft.description, draft.difficulty,
                    draft.starter_code, draft.function_signature,
                )
                if code is not None:
                    draft.optimal_solution = code
            return draft, ProblemChannel.LEETCODE_PULL.value
        return None, ProblemChannel.LEETCODE_PULL.value

    def _unac_from_db(
        self, ctx: GenerationContext, sink: ProgressSink,
    ) -> tuple[ProblemDraft | None, str]:
        """HISTORY：按主题/难度优先选历史未 AC 题。"""
        sink.event(GenEvent("progress", "🔄 正在从历史未 AC 题中选题…"))
        draft = self.store.unac_problem(ctx.topic, ctx.difficulty, ctx.profile_hint)
        return draft, ProblemChannel.DB_UNAC.value

    def _static_fallback(
        self, ctx: GenerationContext, sink: ProgressSink,
    ) -> tuple[ProblemDraft | None, str]:
        """STATIC：本地静态题库兜底（带参 → 无参）。"""
        sink.event(GenEvent("progress", "🔄 正在从静态题库选题…"))
        draft = self.store.static_problem(ctx.topic, ctx.difficulty)
        return draft, ProblemChannel.STATIC.value

    # ── 通道推导 / 持久化 ──
    @staticmethod
    def _channel_of(draft: ProblemDraft | None) -> str | None:
        """统一推导通道：看 from_leetcode（source_slug 或 imported），防止 UI 误导。"""
        if draft is None:
            return None
        if draft.from_leetcode:
            return ProblemChannel.LEETCODE_IMPORT.value
        return ProblemChannel.LLM.value


# 降级链数据化：每步是独立 callable，可单独单测
_FallbackStep = Callable[
    [ProblemGenerationAgent, GenerationContext, ProgressSink],
    tuple[ProblemDraft | None, str],
]
_FALLBACK_CHAIN: list[tuple[str, _FallbackStep]] = [
    ("leetcode_pull", ProblemGenerationAgent._pull_from_leetcode),
    ("db_unac", ProblemGenerationAgent._unac_from_db),
    ("static", ProblemGenerationAgent._static_fallback),
]
