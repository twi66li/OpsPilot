"""定向回归测试：版本化检索缓存、宽召回重排、并发只读工具循环、Skill/Preflight 治理、Webhook 告警接入与探测历史。"""

import json
import threading
import time
from types import SimpleNamespace

from app.agent.skills import effective_tools, list_skills, resolve_skill
from app.agent.tool_loop import run_tool_loop
from app.rag.bm25 import BM25Retriever
from app.rag.embeddings import MockEmbedding
from app.rag.fusion import rrf_fuse
from app.rag.hybrid import HybridRetriever
from app.rag.rerank import HeuristicReranker
from app.rag.vector_store import InMemoryVectorStore, RetrievedChunk
from app.services import website_monitor
from app.services.cache import InMemoryCache
from app.services.preflight import evaluate_remediation_preflight, evaluate_ticket_preflight
from tests.conftest import new_session, register


def test_rrf_and_rerank_do_not_mutate_input_scores():
    c1 = RetrievedChunk(chunk_id="c1", doc_id="d1", doc_title="t1", text="nginx 502 upstream timeout", score=0.91)
    c2 = RetrievedChunk(chunk_id="c2", doc_id="d2", doc_title="t2", text="redis connection refused", score=0.42)
    fused = rrf_fuse([[c1, c2]], k=60, top_k=2)
    assert c1.score == 0.91 and c2.score == 0.42
    assert fused[0].score != 0.91

    reranked = HeuristicReranker().rerank("nginx 502", [c1, c2], top_k=2)
    assert c1.score == 0.91 and c2.score == 0.42
    assert reranked[0].chunk_id == "c1"


def test_hybrid_retriever_versioned_cache_and_wide_rerank_pool():
    cache = InMemoryCache()
    embedder = MockEmbedding(dim=64)
    vs = InMemoryVectorStore(dim=64)
    bm25 = BM25Retriever()

    class CountingEmbedder:
        def __init__(self, inner):
            self.inner = inner
            self.calls = 0

        def embed(self, texts):
            self.calls += 1
            return self.inner.embed(texts)

    counting = CountingEmbedder(embedder)
    docs = [
        {"chunk_id": f"k{i}", "doc_id": f"doc-{i}", "doc_title": f"doc {i}", "text": f"common error log item {i}", "version_id": "v1"}
        for i in range(12)
    ]
    docs.append(
        {
            "chunk_id": "target",
            "doc_id": "doc-target",
            "doc_title": "target",
            "text": "ERR_UNIQUE_POOL_EXHAUSTED maxPoolSize 调优指南",
            "version_id": "v1",
        }
    )
    vecs = embedder.embed([d["text"] for d in docs])
    vs.upsert("t1", docs, vecs)
    bm25.add("t1", docs)

    # 轻量模拟 DB 会话，用于验证投影 active_versions 变化触发缓存自动失效
    active_state = [(d["doc_id"], "v1") for d in docs]

    class FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def filter_by(self, **_kw):
            return self

        def filter(self, *_args):
            return self

        def all(self):
            return list(self._rows)

    class FakeDB:
        def query(self, *entities):
            if len(entities) == 2:
                return FakeQuery(active_state)
            return FakeQuery([])

        def add(self, _row):
            pass

        def commit(self):
            pass

    retriever = HybridRetriever(vs, bm25, counting, HeuristicReranker(), settings=SimpleNamespace(top_k=2), cache=cache)
    db = FakeDB()
    r1 = retriever.retrieve(db, "t1", "ERR_UNIQUE_POOL_EXHAUSTED", top_k=2, log=False)
    assert r1.cache_hit is False and r1.reranked is True
    assert r1.hits[0].chunk_id == "target"
    calls_after_first = counting.calls

    r2 = retriever.retrieve(db, "t1", "ERR_UNIQUE_POOL_EXHAUSTED", top_k=2, log=False)
    assert r2.cache_hit is True
    assert counting.calls == calls_after_first  # 命中缓存，不重复调 embedding
    assert r2.hits[0].chunk_id == "target"

    # 切换文档版本后缓存自动失效
    active_state[-1] = ("doc-target", "v2")
    r3 = retriever.retrieve(db, "t1", "ERR_UNIQUE_POOL_EXHAUSTED", top_k=2, log=False)
    assert r3.cache_hit is False
    assert counting.calls == calls_after_first + 1


def test_tool_loop_parallel_readonly_calls_and_validation_field_hints():
    barrier = threading.Barrier(2, timeout=2.0)

    class TwoCallAPI:
        def __init__(self):
            self.turns = 0
            self.last_messages = []

        def tool_turn(self, messages, _tools, *, allow_tools=True):
            self.turns += 1
            self.last_messages = messages
            if self.turns == 1:
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "query_service_metrics", "arguments": json.dumps({"service": "order-service"})},
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "query_logs", "arguments": json.dumps({"service": "order-service"})},
                        },
                        {
                            "id": "call_bad",
                            "type": "function",
                            "function": {"name": "query_service_metrics", "arguments": json.dumps({"service": "BAD_UPPER!"})},
                        },
                    ],
                }
            return {"role": "assistant", "content": "并发查询已完成"}

    def execute(name, _args):
        barrier.wait()  # 若未并发执行，串行必定死锁触发 BrokenBarrierError
        time.sleep(0.02)
        return {"status": "ok", "tool": name}

    api = TwoCallAPI()
    res = run_tool_loop("同时查询指标与日志", api, execute, max_tool_calls=3)
    assert res["end_reason"] == "answered"
    assert [e["tool_call_id"] for e in res["tool_events"]] == ["call_1", "call_2", "call_bad"]
    assert res["tool_events"][0]["executed"] is True and res["tool_events"][1]["executed"] is True
    bad_result = res["tool_events"][2]["result"]
    assert bad_result["status"] == "invalid_arguments"
    assert any("service" in item for item in bad_result.get("invalid_fields", []))


def test_skills_registry_and_preflight_governance(client, admin_headers, monkeypatch):
    resp = client.get("/api/agent/skills", headers=admin_headers)
    assert resp.status_code == 200
    names = {item["name"] for item in resp.json()}
    assert {"consult", "diagnose", "review", "history", "drill"} <= names
    assert all(item["write_allowed"] is False for item in list_skills())
    assert effective_tools("consult", runtime_allowed=["search_runbook"]) == ("search_runbook",)
    assert resolve_skill("unknown").name == "consult"

    # 预检：默认四眼告警（允许），开启严格模式则阻断自提自批
    soft = evaluate_ticket_preflight(
        {"title": "连接池告警", "severity": "P1", "description": "连接池耗尽详情描述"},
        requested_by="alice",
        approver_username="alice",
        approver_role="engineer",
        strict_four_eyes=False,
    )
    assert soft["passed"] is True
    assert any(c["key"] == "four_eyes" and c["status"] == "warn" for c in soft["checks"])

    strict = evaluate_ticket_preflight(
        {"title": "连接池告警", "severity": "P1", "description": "连接池耗尽详情描述"},
        requested_by="alice",
        approver_username="alice",
        approver_role="engineer",
        strict_four_eyes=True,
    )
    assert strict["passed"] is False
    assert any(c["key"] == "four_eyes" and c["status"] == "fail" for c in strict["checks"])

    rem_bad = evaluate_remediation_preflight(
        "rm_rf_prod",
        service="order-service",
        tenant_id="default",
        requested_by="alice",
        approver_username="bob",
        approver_role="engineer",
    )
    assert rem_bad["passed"] is False

    # 端到端测试：开启 strict_four_eyes 后，同一账号自提自批返回 403
    from app.container import get_container

    session_id = new_session(client, admin_headers)
    chat = client.post(
        "/api/agent/chat",
        json={"session_id": session_id, "message": "订单服务又超时了，请帮我创建工单跟进处理。"},
        headers=admin_headers,
    ).json()
    approval_id = chat["approval"]["approval_id"]
    detail = client.get(f"/api/maintenance/operations/{approval_id}", headers=admin_headers)
    assert detail.status_code == 200 and detail.json()["preflight"]["passed"] is True

    monkeypatch.setattr(get_container().settings, "strict_four_eyes", True)
    blocked = client.post(
        f"/api/agent/approvals/{approval_id}/decide",
        json={"approved": True, "comment": "尝试自提自批"},
        headers=admin_headers,
    )
    assert blocked.status_code == 403


def test_site_webhook_ingress_and_probe_history(client):
    headers = register(client, "webhook_owner_01", "webhook-pass-123")
    site = client.post(
        "/api/support/sites",
        json={"name": "商城主站", "url": "https://shop.example.com/health", "environment": "production"},
        headers=headers,
    ).json()
    site_id = site["id"]

    # 未生成令牌时拒绝接入
    unauth = client.post(f"/api/maintenance/sites/{site_id}/webhook", json={"title": "502"})
    assert unauth.status_code == 401

    # 生成令牌并确保详情接口不泄露哈希或明文
    tok = client.post(f"/api/maintenance/sites/{site_id}/webhook-token", headers=headers)
    assert tok.status_code == 200
    token = tok.json()["token"]
    detail = client.get(f"/api/support/sites/{site_id}", headers=headers).json()
    assert detail["webhook"]["enabled"] is True
    assert "token_sha256" not in detail["webhook"]
    assert token not in json.dumps(detail)

    # 1. Alertmanager 格式告警（含敏感密钥，应自动脱敏）
    am = client.post(
        f"/api/maintenance/sites/{site_id}/webhook",
        headers={"X-OpsPilot-Webhook-Token": token},
        json={
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "High502Rate", "severity": "critical", "endpoint": "/checkout"},
                    "annotations": {"summary": "结账接口 502 password=supersecret123"},
                    "startsAt": "2026-11-19T12:00:00Z",
                }
            ],
        },
    )
    assert am.status_code == 200
    body1 = am.json()
    assert body1["accepted"] is True and body1["created_case"] is True and body1["format"] == "alertmanager"
    assert body1["severity"] == "P0"

    # 2. Uptime Kuma 格式告警应聚合到同一未归档 Webhook 问题
    kuma = client.post(
        f"/api/maintenance/sites/{site_id}/webhook",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "heartbeat": {"status": 0, "time": "2026-11-19T12:05:00Z", "msg": "502 Bad Gateway"},
            "monitor": {"name": "商城健康检查", "url": "https://shop.example.com/health"},
        },
    )
    assert kuma.status_code == 200
    body2 = kuma.json()
    assert body2["created_case"] is False and body2["case_id"] == body1["case_id"] and body2["format"] == "uptime_kuma"

    # 核对证据已脱敏且累积 2 条
    case = client.get(f"/api/support/cases/{body1['case_id']}", headers=headers).json()
    assert len(case["evidence"]) == 2
    assert "supersecret123" not in json.dumps(case, ensure_ascii=False)

    # 核对定时巡检保存 probe_history
    from app.db.base import SessionLocal
    from app.db.models import WebsiteMonitor, WebsiteProfile, _now

    with SessionLocal() as db:
        row = db.get(WebsiteProfile, site_id)
        mon = WebsiteMonitor(
            site_id=site_id,
            enabled=True,
            interval_minutes=15,
            next_check_at=_now(),
            data={"target": "https://shop.example.com/health"},
        )
        db.add(mon)
        db.commit()
        website_monitor.save_result(
            db,
            row,
            mon,
            "https://shop.example.com/health",
            {"state": "normal", "http_status": 200, "elapsed_ms": 88.5, "collected_at": "2026-11-19T12:10:00Z"},
        )
    detail_after = client.get(f"/api/support/sites/{site_id}", headers=headers).json()
    assert len(detail_after["http_monitor"]["probe_history"]) == 1
    assert detail_after["http_monitor"]["probe_history"][0]["elapsed_ms"] == 88.5

    # 吊销令牌后旧令牌立即失效
    assert client.delete(f"/api/maintenance/sites/{site_id}/webhook-token", headers=headers).status_code == 200
    assert (
        client.post(
            f"/api/maintenance/sites/{site_id}/webhook",
            headers={"X-OpsPilot-Webhook-Token": token},
            json={"title": "after revoke"},
        ).status_code
        == 401
    )
