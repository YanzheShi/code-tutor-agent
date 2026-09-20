"""用户反馈（docs/feedback-feature-plan.md Phase 2 / Phase 5）。

覆盖：
- POST /feedback：鉴权 401、**体验账号（role='test'）可提交**、分类白名单、
  内容长度边界（5 / 2000）、纯空白拒绝、strip、身份与 UA 快照落库、
  限频（用户维度 + **IP 维度路由级回归**）
- GET /admin/feedback：未登录 401、普通用户 403、admin 只读列表 / counts /
  分类筛选 / 分页
- 只读口径：确认 PUT/DELETE 与 /feedback/mine 端点**不存在**（锁定 2026-09-20 决策）

⚠️ 全部走 TestClient 真实路由（不直调函数）：
feedback.py 的 IP 限频依赖 `Request` 注入，`from __future__ import annotations`
下漏 import 会**静默注入 None**（`_client_ip(None)=='direct'` → IP 维度失效且零报错）。
单测直调函数或 patch 掉 `_client_ip` 都抓不到这类问题，必须路由级回归
（同 tests/test_trial_user_flow.py::test_trial_ip_anchor_route_level 的教训）。

运行口径：本文件独立跑（python -m pytest tests/test_feedback.py -v），
避免与运行中共库（工作记忆铁律）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from code_tutor_agent.api import auth as auth_mod
from code_tutor_agent.db import database as dbmod

PASSWORD = "password123"


@pytest.fixture()
def temp_db():
    """确保测试 schema 内建表（幂等 init_db）；行级隔离由 conftest _pg_clean_tables 兜底。"""
    dbmod.init_db()
    yield


@pytest.fixture()
def client(temp_db):
    from code_tutor_agent.api.main import app

    with TestClient(app) as c:
        yield c


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_trial(client) -> tuple[dict, str]:
    """建一个体验账号（role='test'），返回 (user, token)。"""
    r = client.post("/auth/trial")
    assert r.status_code == 200, r.text
    body = r.json()
    return body["user"], body["token"]


def _make_user(client, email: str) -> tuple[dict, str]:
    """建一个普通注册用户，返回 (user_row, token)。"""
    uid = dbmod.create_user(email, auth_mod.hash_password(PASSWORD))
    r = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return dbmod.get_user_by_id(uid), r.json()["token"]


def _make_admin(client, email: str = "boss@test.com") -> str:
    dbmod.create_user(email, auth_mod.hash_password(PASSWORD), role="admin")
    r = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["token"]


# ── POST /feedback：鉴权 ─────────────────────────────────────


def test_submit_requires_login(client):
    """无 token → 401（登录即可提交，但必须登录）。"""
    r = client.post("/feedback", json={"category": "bug", "content": "提交按钮没反应"})
    assert r.status_code == 401


def test_submit_requires_valid_token(client):
    """伪造 token → 401。"""
    r = client.post("/feedback", headers={"Authorization": "Bearer not-a-real-token"},
                    json={"category": "bug", "content": "提交按钮没反应"})
    assert r.status_code == 401


# ── POST /feedback：体验账号同权（2026-09-20 决策①） ────────


def test_trial_user_can_submit(client):
    """体验账号（role='test'）与注册用户同权，可提交且快照落到自己的 user_id。"""
    user, token = _make_trial(client)
    assert user["role"] == "test"

    r = client.post("/feedback", headers=_auth(token), json={
        "category": "bug", "content": "手机端导师面板滚动卡顿",
    })
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    assert fid > 0

    row = [x for x in dbmod.list_feedback() if x["id"] == fid][0]
    assert row["user_id"] == user["id"]
    assert row["user_email"] == user["email"]     # 占位邮箱也如实快照


# ── POST /feedback：字段校验 ─────────────────────────────────


def test_submit_rejects_unknown_category(client):
    """分类白名单：只接受 bug / experience / content / other。"""
    _, token = _make_trial(client)
    r = client.post("/feedback", headers=_auth(token),
                    json={"category": "spam", "content": "这是一条足够长的反馈"})
    assert r.status_code == 422


def test_submit_content_length_boundaries(client):
    """边界：4 字拒、5 字过、2000 字过、2001 字拒。"""
    _, token = _make_trial(client)
    h = _auth(token)

    def post(text: str):
        return client.post("/feedback", headers=h, json={"category": "other", "content": text})

    assert post("四个字呀").status_code == 422          # 4 字
    assert post("一二三四五").status_code == 200        # 5 字（下边界）
    assert post("字" * 2000).status_code == 200         # 上边界
    assert post("字" * 2001).status_code == 422         # 超长


def test_submit_rejects_whitespace_only_content(client):
    """纯空白串过得了 Field 的 min_length，必须被 strip 后的校验堵掉。"""
    _, token = _make_trial(client)
    h = _auth(token)
    for blank in ("     ", "\n\n\n\n\n", "  \t  "):
        r = client.post("/feedback", headers=h, json={"category": "other", "content": blank})
        assert r.status_code == 422, f"纯空白未被拒: {blank!r}"


def test_submit_strips_content_and_snapshots_context(client):
    """正文两端空白被 strip；身份 / UA / 上下文快照如实落库。"""
    _, token = _make_trial(client)
    r = client.post("/feedback", headers=_auth(token), json={
        "category": "content",
        "content": "  这道题的描述里有个错别字  ",
        "contact": "wx: yanzhe",
        "screen": "main",
        "problem_id": 42,
        "session_id": "sess-abc",
    })
    assert r.status_code == 200, r.text
    row = dbmod.list_feedback()[0]

    assert row["content"] == "这道题的描述里有个错别字"     # 已 strip
    assert row["category"] == "content"
    assert row["contact"] == "wx: yanzhe"
    assert row["screen"] == "main"
    assert row["problem_id"] == 42
    assert row["session_id"] == "sess-abc"
    assert row["user_agent"], "user_agent 快照应非空"
    assert row["created_at"]


def test_submit_optional_fields_default_to_null(client):
    """可选字段不传 → 存 NULL（不是空串），避免管理端出现两种空值形态。"""
    _, token = _make_trial(client)
    r = client.post("/feedback", headers=_auth(token),
                    json={"category": "other", "content": "只想说一句谢谢"})
    assert r.status_code == 200
    row = dbmod.list_feedback()[0]
    for col in ("contact", "screen", "session_id", "user_agent"):
        if col == "user_agent":
            continue          # TestClient 总会带 UA
        assert row[col] is None, f"{col} 应为 NULL，实际 {row[col]!r}"
    assert row["problem_id"] is None


# ── POST /feedback：限频 ─────────────────────────────────────


def test_rate_limit_per_user(client):
    """用户维度 10 次/小时：第 11 次 → 429。"""
    _, token = _make_trial(client)
    h = _auth(token)
    for i in range(10):
        r = client.post("/feedback", headers=h,
                        json={"category": "other", "content": f"第 {i} 条反馈内容"})
        assert r.status_code == 200, f"第 {i + 1} 次不该被拒: {r.text}"
    r = client.post("/feedback", headers=h,
                    json={"category": "other", "content": "第十一条应该被限频拦下"})
    assert r.status_code == 429
    assert "频繁" in r.json()["detail"]
    assert len(dbmod.list_feedback()) == 10      # 被拒的那条没落库


def test_rate_limit_key_uses_real_client_ip(client, monkeypatch):
    """🎯 专防 Request 静默 None 回归：直接断言限频 key 里的 IP 不是 'direct'。

    feedback.py 里若漏 `from fastapi import Request`，`from __future__ import annotations`
    会把 Request 注解字符串化 → FastAPI 解析失败后**静默注入 None** →
    `_client_ip(None) == 'direct'` → 所有客户端共用一个 "direct" 桶，
    真实 IP 维度完全失效且不报任何错误（session.py 历史上踩过）。
    """
    from code_tutor_agent.api.routers import feedback as fb_mod

    seen: list[str] = []
    monkeypatch.setattr(fb_mod, "rate_limit",
                        lambda key, max_requests, window_sec: seen.append(key))

    _, token = _make_trial(client)
    r = client.post("/feedback", headers=_auth(token),
                    json={"category": "bug", "content": "探测一下 IP 注入"})
    assert r.status_code == 200, r.text

    ip_keys = [k for k in seen if k.startswith("feedback:ip:")]
    assert len(ip_keys) == 1, f"IP 维度限频未执行: {seen}"
    assert not ip_keys[0].endswith(":direct"), (
        f"Request 被静默注入 None，IP 维度限频已失效: {seen}"
    )
    assert ip_keys[0] == "feedback:ip:testclient"      # TestClient 的真实 host


def test_rate_limit_per_ip_across_users(client):
    """IP 维度 30 次/小时：4 个用户（各自用户桶仅 8 次，远未触顶）同 IP 发 31 次，
    第 31 次必须 429 —— 只有 IP 桶被真实计数才会发生。"""
    tokens = [_make_user(client, f"iprl{i}@test.com")[1] for i in range(4)]

    blocked_at = None
    for i in range(31):
        r = client.post("/feedback", headers=_auth(tokens[i % 4]),
                        json={"category": "other", "content": f"批量反馈第 {i} 条"})
        if r.status_code == 429:
            blocked_at = i
            break
        assert r.status_code == 200, f"第 {i + 1} 次不该被拒: {r.text}"

    assert blocked_at == 30, f"IP 桶应在第 31 次触顶，实际第 {blocked_at} 次"


# ── GET /admin/feedback：鉴权 ────────────────────────────────


def test_admin_list_requires_admin(client):
    """未登录 401；普通注册用户 403；体验账号同样 403。"""
    assert client.get("/admin/feedback").status_code == 401

    _, user_tok = _make_user(client, "normal@test.com")
    assert client.get("/admin/feedback", headers=_auth(user_tok)).status_code == 403

    _, trial_tok = _make_trial(client)
    assert client.get("/admin/feedback", headers=_auth(trial_tok)).status_code == 403


# ── GET /admin/feedback：只读视图 ────────────────────────────


def test_admin_list_reads_submitted_feedback(client):
    """admin 能看到用户提交的反馈，含 counts 与倒序。"""
    _, token = _make_trial(client)
    for cat, txt in (("bug", "提交按钮点了没反应"),
                     ("experience", "希望支持 JavaScript"),
                     ("bug", "导师回复偶尔重复两遍")):
        assert client.post("/feedback", headers=_auth(token),
                           json={"category": cat, "content": txt}).status_code == 200

    atok = _make_admin(client)
    d = client.get("/admin/feedback", headers=_auth(atok)).json()

    assert d["total"] == 3
    assert len(d["items"]) == 3
    assert d["counts"] == {"bug": 2, "experience": 1}

    ids = [i["id"] for i in d["items"]]
    assert ids == sorted(ids, reverse=True)

    # 列表带着管理端需要的展示字段
    first = d["items"][0]
    for col in ("id", "user_id", "user_email", "category", "content", "created_at"):
        assert col in first


def test_admin_list_empty_state(client):
    """无反馈时返回空列表 + 空 counts（前端据 total==0 展示空态）。"""
    atok = _make_admin(client)
    d = client.get("/admin/feedback", headers=_auth(atok)).json()
    assert d == {"items": [], "total": 0, "counts": {}}


def test_admin_list_filter_and_paging(client):
    """category 筛选 + limit/offset 分页；counts 始终是全量口径。"""
    _, token = _make_trial(client)
    for i in range(5):
        cat = "bug" if i % 2 else "other"
        assert client.post("/feedback", headers=_auth(token),
                           json={"category": cat, "content": f"第 {i} 条反馈内容"}).status_code == 200

    atok = _make_admin(client)
    h = _auth(atok)

    only_bug = client.get("/admin/feedback", headers=h, params={"category": "bug"}).json()
    assert only_bug["total"] == 2
    assert all(i["category"] == "bug" for i in only_bug["items"])
    # counts 不受 category 参数影响，始终全量（筛选 tab 角标要的是全量）
    assert only_bug["counts"] == {"bug": 2, "other": 3}

    page1 = client.get("/admin/feedback", headers=h, params={"limit": 2, "offset": 0}).json()
    assert len(page1["items"]) == 2 and page1["total"] == 5
    page3 = client.get("/admin/feedback", headers=h, params={"limit": 2, "offset": 4}).json()
    assert len(page3["items"]) == 1
    assert page3["items"][0]["id"] < page1["items"][0]["id"]   # 倒序翻页

    # 非法 category / 越界 limit → 422
    assert client.get("/admin/feedback", headers=h, params={"category": "spam"}).status_code == 422
    assert client.get("/admin/feedback", headers=h, params={"limit": 999}).status_code == 422


# ── 只读口径：变更类端点必须不存在 ───────────────────────────

def test_no_mutation_or_mine_endpoints(client):
    """锁定 2026-09-20 决策：管理端只读、不做用户侧回显。

    - 管理端无 PUT / DELETE / 状态流转端点
    - 用户侧无 GET /feedback/mine
    """
    _, token = _make_trial(client)
    atok = _make_admin(client)

    assert client.put("/admin/feedback/1", headers=_auth(atok), json={}).status_code == 404
    assert client.delete("/admin/feedback/1", headers=_auth(atok)).status_code == 404
    assert client.get("/feedback/mine", headers=_auth(token)).status_code == 404


# ── 埋点（Phase 6） ─────────────────────────────────────────


def test_submit_records_metric(client):
    """提交成功记一次 feedback_submitted（Prometheus 桥接 → cta_feedback_submitted_total）。"""
    from code_tutor_agent.monitoring.metrics import get_registry

    reg = get_registry()
    before = reg.snapshot()["counters"].get("feedback_submitted", 0)

    _, token = _make_trial(client)
    assert client.post("/feedback", headers=_auth(token),
                       json={"category": "bug", "content": "埋点验证用的一条反馈"}).status_code == 200

    assert reg.snapshot()["counters"].get("feedback_submitted", 0) == before + 1


def test_failed_submit_does_not_count_metric(client):
    """落库失败（create_feedback 返回 0 → 500）不应记成功埋点。

    埋点必须排在 create_feedback 之后。
    """
    from code_tutor_agent.monitoring.metrics import get_registry

    from code_tutor_agent.db import database as _dbmod

    _, token = _make_trial(client)
    reg = get_registry()
    before = reg.snapshot()["counters"].get("feedback_submitted", 0)

    original = _dbmod.create_feedback
    _dbmod.create_feedback = lambda *a, **k: 0  # type: ignore[assignment]
    try:
        # feedback.py 在函数内 import create_feedback，patch 模块属性即可生效
        r = client.post("/feedback", headers=_auth(token),
                        json={"category": "bug", "content": "这条会落库失败"})
        assert r.status_code == 500
        assert reg.snapshot()["counters"].get("feedback_submitted", 0) == before
    finally:
        _dbmod.create_feedback = original  # type: ignore[assignment]
