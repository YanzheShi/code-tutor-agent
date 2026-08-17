"""Test: error-mode 6 维弱点画像的纯函数（weakness.py）。

覆盖：枚举/校验、冷启动、衰减、加权 severity 合并、封顶、未知 slug 丢弃、
判题失败 ×1.3 boost。全程无 IO、无 LLM。
"""
from __future__ import annotations

from code_tutor_agent.profile.weakness import (
    DIM_KEYS,
    VALID_TAGS,
    WEAKNESS_TAGS,
    apply_deltas,
    boost_verdict_deltas,
    empty_error_modes,
    is_valid_tag,
    ErrorModeDelta,
)


def _delta(dim="correctness", tag="boundary", delta_count=1, severity=0.5, evidence=""):
    return ErrorModeDelta(dim=dim, tag=tag, delta_count=delta_count,
                          severity=severity, evidence=evidence)


class TestEnumAndValidate:
    def test_six_dims(self):
        assert DIM_KEYS == ["correctness", "datastruct", "perf", "algo", "impl", "debug"]

    def test_valid_tags_all_dimensions_have_slugs(self):
        assert all(len(v) > 0 for v in WEAKNESS_TAGS.values())
        assert len(VALID_TAGS) == sum(len(v) for v in WEAKNESS_TAGS.values())

    def test_is_valid_tag_known(self):
        assert is_valid_tag("correctness", "boundary") is True
        assert is_valid_tag("debug", "traj_stuck") is True

    def test_is_valid_tag_unknown_dim(self):
        assert is_valid_tag("nope", "boundary") is False

    def test_is_valid_tag_unknown_tag(self):
        assert is_valid_tag("correctness", "not_a_real_tag") is False

    def test_empty_error_modes_shape(self):
        em = empty_error_modes()
        assert set(em.keys()) == set(DIM_KEYS)
        assert all(em[d] == {} for d in DIM_KEYS)


class TestColdStart:
    def test_first_delta_count_is_one(self):
        out = apply_deltas({}, [_delta(delta_count=1, severity=0.5)])
        item = out["correctness"]["boundary"]
        assert item["count"] == 1.0
        # severity = 0*0.6 + 0.5*0.4
        assert item["severity"] == 0.2

    def test_first_delta_records_last_seen_and_evidence(self):
        out = apply_deltas({}, [_delta(evidence="for-loop off by one")])
        item = out["correctness"]["boundary"]
        assert item["last_seen"]
        assert item["evidence"] == "for-loop off by one"


class TestAccumulateAndDecay:
    def test_two_deltas_same_tag_accumulate(self):
        # 一次分析内同一 tag 暴露两次 → 衰减后的旧值 + 两次 delta 累加
        out = apply_deltas({}, [_delta(delta_count=1, severity=0.5),
                                _delta(delta_count=1, severity=0.5)])
        item = out["correctness"]["boundary"]
        # 旧为空，两次 delta 各 +1 → count = 2.0
        assert item["count"] == 2.0
        # sev: 0 + 0.5*0.4 再 + 0.5*0.4 = 0.4
        assert item["severity"] == 0.4

    def test_decay_without_new_hits(self):
        import pytest
        once = apply_deltas({}, [_delta(delta_count=3, severity=0.5)])
        # 连续 3 次空增量 → 衰减 0.85^3（每步四舍五入到 3 位，允许 0.005 误差）
        out = once
        for _ in range(3):
            out = apply_deltas(out, [])
        item = out["correctness"]["boundary"]
        assert item["count"] == pytest.approx(3 * (0.85 ** 3), abs=0.005)

    def test_other_dims_untouched(self):
        out = apply_deltas({}, [_delta(dim="perf", tag="tle_brute", delta_count=2)])
        assert "boundary" not in out["correctness"]
        assert out["perf"]["tle_brute"]["count"] == 2.0


class TestSeverityMerge:
    def test_severity_blends_toward_new(self):
        # 先低 severity，再补高 severity，应更接近高
        low = apply_deltas({}, [_delta(delta_count=1, severity=0.2)])
        assert low["correctness"]["boundary"]["severity"] == 0.08  # 0.2*0.4
        high = apply_deltas(low, [_delta(delta_count=1, severity=1.0)])
        assert high["correctness"]["boundary"]["severity"] == 0.448  # 0.08*0.6 + 1.0*0.4
        assert high["correctness"]["boundary"]["severity"] < 0.5

    def test_severity_capped_at_one(self):
        out = apply_deltas({}, [_delta(delta_count=5, severity=1.0)])
        for _ in range(10):
            out = apply_deltas(out, [_delta(delta_count=1, severity=1.0)])
        assert out["correctness"]["boundary"]["severity"] <= 1.0


class TestCap:
    def test_all_legal_slugs_retained_no_cap(self):
        # 全固化枚举下每个 dim 合法 slug 上限为 5（correctness），
        # apply_deltas 不再有运行期封顶分支；一次把所有 correctness slug 灌入，
        # 应全部保留（结构上限即天然约束）。
        deltas = [_delta(dim="correctness", tag=t, delta_count=1, severity=0.5)
                  for t in WEAKNESS_TAGS["correctness"]]
        out = apply_deltas({}, deltas)
        assert len(out["correctness"]) == len(WEAKNESS_TAGS["correctness"])
        for t in WEAKNESS_TAGS["correctness"]:
            assert t in out["correctness"]

    def test_old_dirty_keys_preserved_verbatim(self):
        # apply_deltas 对 old 中的 key 不做 slug 校验（原样拷贝后衰减）；
        # 即便塞入非枚举 key，也应随衰减保留，而不是被封顶分支误删。
        old = {d: {} for d in DIM_KEYS}
        old["correctness"] = {
            "a": {"count": 10.0, "severity": 0.9, "last_seen": "", "evidence": ""},
            "b": {"count": 1.0,  "severity": 0.1, "last_seen": "", "evidence": ""},
        }
        out = apply_deltas(old, [])  # 无新 delta，仅时间衰减
        assert "a" in out["correctness"] and "b" in out["correctness"]
        # 衰减生效：count ×0.85, severity ×0.6
        assert out["correctness"]["a"]["count"] == 8.5
        assert out["correctness"]["a"]["severity"] == 0.54


class TestInvalidDropped:
    def test_unknown_dim_dropped(self):
        out = apply_deltas({}, [_delta(dim="ghost", tag="x", delta_count=1)])
        assert "ghost" not in out  # 未知维度整体不出现在结果
        # 其他维度不应被污染
        assert out["correctness"] == {}

    def test_unknown_tag_dropped(self):
        out = apply_deltas({}, [_delta(dim="correctness", tag="ghost_tag")])
        assert "ghost_tag" not in out["correctness"]

    def test_valid_and_invalid_mixed(self):
        out = apply_deltas({}, [
            _delta(dim="correctness", tag="boundary", delta_count=1),
            _delta(dim="correctness", tag="bogus", delta_count=1),
        ])
        assert "boundary" in out["correctness"]
        assert "bogus" not in out["correctness"]


class TestVerdictBoost:
    def test_boost_scales_count_and_severity(self):
        deltas = [_delta(delta_count=10, severity=0.5)]
        boosted = boost_verdict_deltas(deltas, multiplier=1.3)
        assert boosted[0].delta_count == 13          # round(10*1.3)
        assert boosted[0].severity == 0.65           # 0.5*1.3

    def test_boost_severity_capped(self):
        deltas = [_delta(delta_count=1, severity=1.0)]
        boosted = boost_verdict_deltas(deltas, multiplier=1.3)
        assert boosted[0].severity == 1.0

    def test_boost_preserves_dim_tag_evidence(self):
        d = _delta(dim="perf", tag="tle_brute", delta_count=2, severity=0.3, evidence="TLE")
        b = boost_verdict_deltas([d])[0]
        assert b.dim == "perf" and b.tag == "tle_brute" and b.evidence == "TLE"

    def test_boosted_merge_increases_profile(self):
        # count 用较大值（10）以便 boost 后的整数倍可见（round(10*1.3)=13）
        base = apply_deltas({}, [_delta(dim="perf", tag="tle_brute", delta_count=10, severity=0.5)])
        boosted = boost_verdict_deltas([_delta(dim="perf", tag="tle_brute", delta_count=10, severity=0.5)])
        merged = apply_deltas(base, boosted)
        item = merged["perf"]["tle_brute"]
        # count: 10 → 10*0.85 + round(10*1.3)=13 → 8.5 + 13 = 21.5
        assert item["count"] == 21.5
        # sev: 0.2 → 0.2*0.6 + (0.5*1.3)*0.4 = 0.12 + 0.26 = 0.38
        assert item["severity"] == 0.38
