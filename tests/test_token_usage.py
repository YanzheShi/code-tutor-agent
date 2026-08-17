"""Test: Token 成本计量链路(零侵入采集 + 落库 + 聚合 + 路由)。

覆盖：
- cost:       单价折算 / 缓存命中率 / 模型匹配 / 分类映射
- callback:   usage_metadata 提取 3 条路径 + 旁路 enqueue 正确归因
- sink:       预聚合 + 批量落库(daily UPSERT)
- database:   明细写 + 概览/用途/缓存/预算/明细 聚合查询
- router:     /admin/token/* 路由 + 密码校验 + 响应结构与前端契约一致

全部使用临时库，不触碰 dev DB；不依赖真实 LLM 网关。
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import pytest

from code_tutor_agent.db import database as db
from code_tutor_agent.token_usage import cost

# 测试对「今日」落库的明细做日期过滤,必须用真实系统日期(落库 ts 取本地 now),
# 不能硬编码,否则跨天后过滤落空。
TODAY = datetime.now().strftime("%Y-%m-%d")


def _date_ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


# ── fixtures ──
@pytest.fixture
def tmp_db(monkeypatch):
    """把 DB_PATH 指到临时文件并初始化表结构(含 token 两张表)。"""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "test_code_tutor.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    yield path


@pytest.fixture
def sample_rows():
    """两行样例明细(purpose / 缓存命中差异),用于聚合查询断言。"""
    return [
        ("s1", "default", "judge", "api-judge", "deepseek-chat", "agent",
         "dp", "medium", 1, 1000, 500, 0, 200, 1700, 1.82, 320, "r1"),
        ("s1", "default", "problem", "api-problem", "deepseek-chat", "agent",
         "array", "easy", 2, 2000, 1000, 100, 500, 3600, 3.71, 510, "r2"),
    ]


# ── cost ──
class TestCost:
    def test_cost_calculator_four_tier(self):
        rec = {
            "prompt_tokens": 1000, "completion_tokens": 500,
            "cache_creation_tokens": 0, "cache_read_tokens": 200,
        }
        # (800 non-cached*1 + 500*2 + 0*1.25 + 200*0.1)/1000
        assert cost.cost_calculator("deepseek-chat", rec) == pytest.approx(1.82)

    def test_cost_calculator_cache_creation(self):
        rec = {
            "prompt_tokens": 1000, "completion_tokens": 100,
            "cache_creation_tokens": 400, "cache_read_tokens": 0,
        }
        # (600*1 + 100*2 + 400*1.25 + 0)/1000 = (600+200+500)/1000
        assert cost.cost_calculator("deepseek-chat", rec) == pytest.approx(1.3)

    def test_pick_pricing_unknown_falls_to_default(self):
        assert cost.pick_pricing("gpt-4o") is cost.DEFAULT_PRICING
        assert cost.pick_pricing("deepseek-reasoner")["output"] == 16.0

    def test_cache_hit_rate(self):
        rec = {"prompt_tokens": 1000, "cache_creation_tokens": 100, "cache_read_tokens": 200}
        # 200 / (200 + (1000-100-200)) = 200/900
        assert cost.cache_hit_rate(rec) == pytest.approx(200 / 900)
        # 兼容聚合 totals 的简写键名
        rec_short = {"prompt": 1000, "cache_creation": 100, "cache_read": 200}
        assert cost.cache_hit_rate(rec_short) == pytest.approx(200 / 900)
        # 全命中
        full = {"prompt_tokens": 300, "cache_creation_tokens": 0, "cache_read_tokens": 300}
        assert cost.cache_hit_rate(full) == pytest.approx(1.0)
        # 无用量
        empty = {"prompt_tokens": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0}
        assert cost.cache_hit_rate(empty) == 0.0

    def test_category_of(self):
        assert cost.category_of("judge") == "判题"
        assert cost.category_of("dialog") == "对话"
        assert cost.category_of("totally-unknown") == "其他"


# ── callback ──
class TestCallback:
    def _fake_result(self, usage):
        class Msg:
            usage_metadata = usage

        class Gen:
            message = Msg()

        class LLMResult:
            generations = [[Gen()]]

        return LLMResult()

    def test_extract_usage_from_message(self):
        from code_tutor_agent.token_usage.callback import TokenUsageCallbackHandler

        # langchain-core ≥0.3:缓存信息在 input_token_details,顶层旧键已废弃
        um = {"input_tokens": 10, "output_tokens": 5,
              "input_token_details": {"cache_read": 2, "cache_creation": 1},
              "total_tokens": 18}
        handler = TokenUsageCallbackHandler()
        got = handler._extract_usage(self._fake_result(um), {})
        assert got["input_tokens"] == 10
        assert got["input_token_details"]["cache_read"] == 2

    def test_extract_cache_tokens_new_and_legacy(self):
        from code_tutor_agent.token_usage.callback import _extract_cache_tokens

        # 新布局:input_token_details
        assert _extract_cache_tokens(
            {"input_token_details": {"cache_read": 30, "cache_creation": 5}}
        ) == (5, 30)
        # 兜底:旧顶层键
        assert _extract_cache_tokens(
            {"cache_read_input_tokens": 40, "cache_creation_input_tokens": 10}
        ) == (10, 40)
        # 都缺 → 0
        assert _extract_cache_tokens({"input_tokens": 10}) == (0, 0)

    def test_extract_usage_from_openai_llm_output(self):
        from code_tutor_agent.token_usage.callback import TokenUsageCallbackHandler

        class LLMResult:
            generations = []
            llm_output = {"usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                    "prompt_tokens_details": {"cached_tokens": 30},
                                    "total_tokens": 180}}

        handler = TokenUsageCallbackHandler()
        got = handler._extract_usage(LLMResult(), {})
        assert got["input_tokens"] == 100
        assert got["cache_read_input_tokens"] == 30

    def test_extract_usage_deepseek_style_cache(self):
        from code_tutor_agent.token_usage.callback import TokenUsageCallbackHandler

        class LLMResult:
            generations = []
            llm_output = {"usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                    "prompt_cache_hit_tokens": 25,
                                    "prompt_cache_miss_tokens": 75,
                                    "total_tokens": 150}}

        handler = TokenUsageCallbackHandler()
        got = handler._extract_usage(LLMResult(), {})
        assert got["cache_read_input_tokens"] == 25

    def test_handle_enqueues_attributed_record(self, monkeypatch):
        """走真实配对:on_llm_start 缓存 metadata,on_llm_end 按 run_id 归因。"""
        from code_tutor_agent.token_usage import callback

        captured = []

        class FakeSink:
            def enqueue(self, rec):
                captured.append(rec)

        monkeypatch.setattr(callback, "get_token_sink", lambda: FakeSink())

        um = {"input_tokens": 1000, "output_tokens": 500,
              "input_token_details": {"cache_read": 200, "cache_creation": 0},
              "total_tokens": 1700}
        meta = {"purpose": "judge", "model_name": "deepseek-chat", "model_alias": "api-judge",
                "session_id": "s1", "mode": "agent", "topic": "dp", "problem_id": 1}
        handler = callback.TokenUsageCallbackHandler()
        handler.on_llm_start({}, ["prompt"], run_id="r1", metadata=meta)
        handler.on_llm_end(self._fake_result(um), run_id="r1")
        assert len(captured) == 1
        rec = captured[0]
        assert rec["purpose"] == "judge"
        assert rec["session_id"] == "s1"
        assert rec["model_name"] == "deepseek-chat"
        assert rec["prompt_tokens"] == 1000
        assert rec["cache_read_tokens"] == 200
        assert rec["cost"] == pytest.approx(1.82)
        # 消费后已清理,避免跨调用串 metadata
        assert "r1" not in handler._run_meta

    def test_handle_metadata_start_end_pairing(self):
        """真实归因链路:on_llm_start(携带 llm 级 + graph 级合并的 metadata)
        → 按 run_id 缓存 → on_llm_end 取回。

        与 LangChain 行为一致:metadata 只在 on_llm_start 携带(见
        langchain_core manager.py:1412),on_llm_end 不带 metadata(839/1192)。
        本测试断言 start/end 配对后,业务维度与会话维度同时归因,防失真。
        """
        from code_tutor_agent.token_usage import callback

        captured = []

        class FakeSink:
            def enqueue(self, rec):
                captured.append(rec)

        callback.get_token_sink = lambda: FakeSink()

        um = {"input_tokens": 600, "output_tokens": 200,
              "input_token_details": {"cache_read": 100, "cache_creation": 0},
              "total_tokens": 900}
        # LangChain 在 start 时已把 llm 级(with_config) 与 graph 级(build_run_config)
        # 合并为一份 metadata 传入;此处直接模拟这份合并后的 metadata
        merged_meta = {
            "purpose": "tutor-eval", "model_name": "deepseek-chat", "model_alias": "default",
            "session_id": "s_merge", "mode": "agent", "topic": "dp", "problem_id": 7,
        }
        handler = callback.TokenUsageCallbackHandler()
        handler.on_llm_start({}, ["prompt"], run_id="r9", metadata=merged_meta)
        # on_llm_end 不传 metadata,只靠 run_id 取回
        handler.on_llm_end(self._fake_result(um), run_id="r9")
        assert len(captured) == 1
        rec = captured[0]
        assert rec["purpose"] == "tutor-eval"
        assert rec["model_alias"] == "default"
        assert rec["session_id"] == "s_merge"
        assert rec["mode"] == "agent"
        assert rec["topic"] == "dp"
        assert rec["problem_id"] == 7


# ── sink ──
class TestSink:
    def test_aggregate_daily(self):
        from code_tutor_agent.token_usage.sink import TokenSink

        batch = [
            {"ts_day": TODAY, "purpose": "judge", "model_alias": "a",
             "user_id": "default", "prompt_tokens": 100, "completion_tokens": 50,
             "cache_creation_tokens": 0, "cache_read_tokens": 20, "cost": 0.1},
            {"ts_day": TODAY, "purpose": "judge", "model_alias": "a",
             "user_id": "default", "prompt_tokens": 200, "completion_tokens": 80,
             "cache_creation_tokens": 0, "cache_read_tokens": 40, "cost": 0.2},
            {"ts_day": TODAY, "purpose": "problem", "model_alias": "b",
             "user_id": "default", "prompt_tokens": 500, "completion_tokens": 100,
             "cache_creation_tokens": 10, "cache_read_tokens": 100, "cost": 0.5},
        ]
        daily = TokenSink._aggregate_daily(batch)
        # 2 distinct buckets
        assert len(daily) == 2
        bucket_judge = next(b for b in daily if b[1] == "judge")
        # (day, purpose, model_alias, user_id, call_count, ...)
        assert bucket_judge[4] == 2
        assert bucket_judge[5] == 300  # prompt
        assert bucket_judge[9] == pytest.approx(0.3)  # cost

    def test_drain_writes_to_db(self, tmp_db, monkeypatch):
        from code_tutor_agent.token_usage.sink import TokenSink

        sink = TokenSink()
        rec = {
            "session_id": "s1", "user_id": "default", "purpose": "judge",
            "model_alias": "api-judge", "model_name": "deepseek-chat", "mode": "agent",
            "topic": "dp", "difficulty": "medium", "problem_id": 1,
            "prompt_tokens": 1000, "completion_tokens": 500,
            "cache_creation_tokens": 0, "cache_read_tokens": 200,
            "total_tokens": 1700, "cost": 1.82, "latency_ms": 320, "run_id": "r1",
            "ts_day": TODAY,
        }
        # 直接 drain 同步落库,不依赖后台线程时序
        sink.enqueue(rec)
        sink._drain()

        rows = db.query_token_usage_recent(limit=10)
        assert len(rows) == 1
        assert rows[0]["purpose"] == "judge"
        assert rows[0]["cost"] == pytest.approx(1.82)


# ── database aggregation ──
class TestDatabaseAggregation:
    def _seed(self, rows):
        db.insert_token_usage_batch(rows)

    def test_overview_shape(self, tmp_db, sample_rows):
        self._seed(sample_rows)
        out = db.query_token_overview(TODAY, TODAY)
        assert set(out) >= {"kpis", "trend", "tokenTrend", "moduleShare",
                            "moduleTokenShare", "topPurposes"}
        assert len(out["kpis"]) == 9
        kpi_labels = {k["label"] for k in out["kpis"]}
        assert {"总成本", "总调用", "缓存命中率", "预估月费", "总 Token",
                "输入 Token", "输出 Token", "缓存读", "缓存写"} <= kpi_labels
        assert all("delta" in k for k in out["kpis"])
        assert out["range"].get("model") == "全部"
        assert out["moduleShare"][0]["purpose"] in {"judge", "problem"}
        assert "pct" in out["moduleShare"][0]
        assert out["moduleTokenShare"][0]["purpose"] in {"judge", "problem"}
        assert out["moduleTokenShare"][0]["tokens"] > 0
        assert set(out["tokenTrend"][0]) >= {"day", "prompt", "completion",
                                             "cache_read", "cache_creation"}

    def test_overview_kpi_follows_range(self, tmp_db, sample_rows):
        # KPI 口径随范围联动:近30天(含全部样本)的"总 Token"应为当日口径之和
        self._seed(sample_rows)
        day = db.query_token_overview(TODAY, TODAY)
        week = db.query_token_overview(_date_ago(6), TODAY)
        assert week["kpis"][4]["value"] == day["kpis"][4]["value"]  # 样本全在今天
        assert week["kpis"][0]["value"] >= day["kpis"][0]["value"]

    def test_purposes_rows(self, tmp_db, sample_rows):
        self._seed(sample_rows)
        rows = db.query_token_purposes(TODAY, TODAY)
        assert len(rows) == 2
        r = rows[0]
        for f in ("purpose", "category", "calls", "promptK", "completionK",
                  "cacheReadK", "hit", "cost", "delta"):
            assert f in r
        assert r["category"] in {"判题", "出题"}

    def test_cache_rows_and_diagnosis(self, tmp_db):
        # 命中率 < 40% 应给出 tip
        low_hit = ("s1", "default", "memory-extract", "api-mem", "deepseek-chat", "agent",
                   "", "", 0, 1000, 100, 0, 50, 1150, 1.0, 200, "r1")
        db.insert_token_usage_batch([low_hit])
        rows = db.query_token_cache(TODAY, TODAY)
        mem = next(r for r in rows if r["purpose"] == "memory-extract")
        assert mem["hit"] < 40
        assert mem["tip"] is not None

    def test_budget_sums_today(self, tmp_db, sample_rows, monkeypatch):
        monkeypatch.setenv("TOKEN_DAILY_BUDGET", "50")
        monkeypatch.setenv("TOKEN_SESSION_BUDGET", "5")
        self._seed(sample_rows)
        out = db.query_token_budget()
        assert set(out) >= {"budgets", "alerts"}
        assert out["budgets"][0]["name"] == "你的日预算（总额）"
        assert out["budgets"][0]["limit"] == 50.0
        # 当日 total cost = 1.82 + 3.71 = 5.53
        assert out["budgets"][0]["used"] == pytest.approx(5.53)
        # 三层:日总额 / 用户日 / 单 Session
        assert out["budgets"][1]["name"] == "用户日预算（每人）"
        assert out["budgets"][2]["name"] == "单 Session 预算"

    def test_usage_recent_and_csv(self, tmp_db, sample_rows):
        self._seed(sample_rows)
        rows = db.query_token_usage_recent(limit=10)
        assert len(rows) == 2
        assert {"ts", "session_id", "purpose", "prompt_tokens", "cost"} <= set(rows[0])
        csv = db.export_token_usage_csv()
        assert csv.startswith("ts,session_id,purpose")
        assert "judge" in csv and "problem" in csv

    def test_date_filter(self, tmp_db):
        db.insert_token_usage_batch([
            ("s1", "default", "judge", "a", "deepseek-chat", "agent", "", "", 0,
             100, 10, 0, 5, 115, 0.1, 100, "r1"),
        ])
        # 范围外日期应返回空
        assert db.query_token_usage_recent(limit=10, from_date="2000-01-01",
                                           to_date="2000-01-02") == []


# ── router integration ──
class TestRouter:
    @pytest.mark.asyncio
    async def test_overview_requires_password_and_shape(self, tmp_db, sample_rows, monkeypatch):
        from code_tutor_agent.api.routers import token as token_router
        from code_tutor_agent.schemas.api import TokenStatsRequest

        monkeypatch.setenv("ADMIN_PASSWORD", "secret")
        db.insert_token_usage_batch(sample_rows)

        # 错误密码 → 401
        with pytest.raises(Exception):
            await token_router.token_overview(TokenStatsRequest(password="wrong",
                                                                from_date=TODAY,
                                                                to_date=TODAY))

        # 正确密码 → 结构与前端契约一致
        out = await token_router.token_overview(TokenStatsRequest(password="secret",
                                                                  from_date=TODAY,
                                                                  to_date=TODAY))
        assert "kpis" in out and "moduleShare" in out and "topPurposes" in out

    @pytest.mark.asyncio
    async def test_purposes_cache_budget_usage_endpoints(self, tmp_db, sample_rows, monkeypatch):
        from code_tutor_agent.api.routers import token as token_router
        from code_tutor_agent.schemas.api import TokenStatsRequest

        monkeypatch.setenv("ADMIN_PASSWORD", "secret")
        monkeypatch.setenv("TOKEN_DAILY_BUDGET", "50")
        monkeypatch.setenv("TOKEN_SESSION_BUDGET", "5")
        db.insert_token_usage_batch(sample_rows)
        body = TokenStatsRequest(password="secret", from_date=TODAY, to_date=TODAY)

        purposes = await token_router.token_purposes(body)
        assert "rows" in purposes and len(purposes["rows"]) == 2
        cache = await token_router.token_cache(body)
        assert "rows" in cache
        budget = await token_router.token_budget(TokenStatsRequest(password="secret"))
        assert "budgets" in budget and "alerts" in budget
        usage = await token_router.token_usage(body)
        assert "rows" in usage and len(usage["rows"]) == 2

    @pytest.mark.asyncio
    async def test_csv_export_requires_password_and_post(self, tmp_db, sample_rows, monkeypatch):
        """导出改 POST:密码走 body,不在 URL;错误密码 401,正确密码返回 csv body。"""
        from code_tutor_agent.api.routers import token as token_router
        from code_tutor_agent.schemas.api import TokenStatsRequest

        monkeypatch.setenv("ADMIN_PASSWORD", "secret")
        db.insert_token_usage_batch(sample_rows)

        with pytest.raises(Exception):
            await token_router.token_usage_export(
                TokenStatsRequest(password="nope", from_date=TODAY, to_date=TODAY))
        resp = await token_router.token_usage_export(
            TokenStatsRequest(password="secret", from_date=TODAY, to_date=TODAY))
        csv_text = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
        assert "judge" in csv_text
