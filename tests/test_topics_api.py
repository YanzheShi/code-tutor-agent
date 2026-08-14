"""GET /topics — 主题目录接口单测。

只挂 problems 路由（不 import 全量 app，避免编译 LangGraph），
用最小 FastAPI app + TestClient 验证接口契约。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_tutor_agent.api.routers import problems as problems_router
from code_tutor_agent.topics import TOPIC_CATALOG


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(problems_router.router)
    return TestClient(app)


def test_topics_endpoint_shape():
    r = _client().get("/topics")
    assert r.status_code == 200
    data = r.json()
    assert "topics" in data
    assert isinstance(data["topics"], list)
    assert len(data["topics"]) == len(TOPIC_CATALOG) >= 20
    for t in data["topics"]:
        assert set(t) == {"value", "label"}
        assert t["value"] == t["label"]  # 值即中文主题名（生成链路以中文名为键）
        assert t["value"] in TOPIC_CATALOG


def test_topics_catalog_no_alias_duplicates():
    # 目录不允许别名重复（如「图」与「图论」并存会让按钮重复且语义模糊）
    assert len(TOPIC_CATALOG) == len(set(TOPIC_CATALOG))
    # 每个主题都应能被生成链路映射（generator._TOPIC_TAG_MAP 或默认回退均不抛错）
    from code_tutor_agent.nodes.generator import tag_for

    for t in TOPIC_CATALOG:
        assert tag_for(t), t
