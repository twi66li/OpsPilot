from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.agent.llm import OpenAIChatLLM
from app.api import router_kb
from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import AgentRun, ChatSession, SupportCase, Ticket, _now
from app.rag import hybrid
from app.rag.bm25 import BM25Retriever
from app.services import cache
from tests.conftest import new_session, register, seed_tenant_admin


def test_bm25_reads_chunks_under_the_index_lock():
    retriever = BM25Retriever()
    row = {"chunk_id": "chunk", "doc_id": "runbook", "doc_title": "手册", "text": "maxPoolSize"}
    retriever.add("tenant", [row])

    class CheckedBucket(dict):
        def values(self):
            assert retriever._lock.locked()
            return super().values()

    retriever._chunks["tenant"] = CheckedBucket(retriever._chunks["tenant"])
    assert [hit.chunk_id for hit in retriever.search("tenant", "maxPoolSize")] == ["chunk"]


def test_parent_window_preserves_search_clock(monkeypatch):
    clock = iter([10000.0, 10000.012])
    monkeypatch.setattr(hybrid, "time", SimpleNamespace(perf_counter=lambda: next(clock)))
    hit = SimpleNamespace(text="match", extra={"parent_id": "parent"})
    db = Mock()
    db.query.return_value.filter_by.return_value.all.return_value = []
    db.query.return_value.filter.return_value.all.return_value = [
        SimpleNamespace(id="parent", text="x" * 800 + "match")
    ]
    retriever = hybrid.HybridRetriever(None, Mock(search=Mock(return_value=[hit])), None)
    result = retriever.retrieve(db, "tenant", "match", mode="bm25", log=False)
    assert hit.extra["matched_text"] == "match" and len(hit.text) == 505
    assert result.latency_ms == pytest.approx(12.0)


@pytest.mark.parametrize(("text", "expected"), [
    ('```json\n{"ok": true}\n``` [1] {"other": 2}', {"ok": True}),
    ('[1, {"ignored": 2}] {"value": "}"}', {"value": "}"}),
    ('[{"ignored": 2}]', None),
    ('说明 {无法解析}', None),
])
def test_model_json_returns_first_object(text, expected):
    assert OpenAIChatLLM._parse_json(text) == expected


def test_legacy_timeline_falls_back_and_sorts_in_utc(client, admin_headers):
    case = client.post("/api/support/cases", json={"title": "历史事件"}, headers=admin_headers).json()
    with SessionLocal() as db:
        row = db.get(SupportCase, case["id"])
        row.data = {**row.data, "evidence": [
            {"id": "a", "label": "较早", "submitted_at": "2025-01-01T08:03:00+08:00"},
            {"id": "b", "label": "较晚", "submitted_at": "2025-01-01T00:05:00Z"},
            {"id": "c", "label": "无效", "submitted_at": "invalid"},
        ]}
        created_at = row.created_at
        db.commit()
    resp = client.get(f"/api/maintenance/cases/{case['id']}/timeline", headers=admin_headers)
    assert resp.status_code == 200
    old_events = [e for e in resp.json()["events"] if str(e["id"]).startswith("legacy-")]
    assert [e["id"] for e in old_events] == ["legacy-a", "legacy-b", "legacy-c"]
    assert old_events[0]["at"] == "2025-01-01T00:03:00Z"
    assert old_events[2]["at"] == created_at.replace(tzinfo=None).isoformat() + "Z"


def test_private_upload_limits_and_preserves_duplicates(client, viewer_headers, monkeypatch, tmp_path):
    from app.container import get_container

    monkeypatch.setattr(get_container().settings, "kb_storage_path", str(tmp_path))
    monkeypatch.setattr(router_kb, "enqueue", lambda *args: None)
    monkeypatch.setattr(router_kb, "FILE_VERSIONS_PER_NAMESPACE", 1)
    monkeypatch.setattr(router_kb, "UPLOADS_PER_MINUTE", 3)
    fixed_time = cache.time.time()
    monkeypatch.setattr(cache, "time", SimpleNamespace(time=lambda: fixed_time))

    def upload(content):
        return client.post("/api/support/documents/upload", headers=viewer_headers,
                           files={"file": ("runbook.txt", content)})

    created = upload(b"runbook")
    assert created.status_code == 202, created.text
    duplicate = upload(b"runbook")
    assert duplicate.status_code == 202 and duplicate.json()["duplicate"]
    assert duplicate.json()["version_id"] == created.json()["version_id"]
    assert upload(b"another").status_code == 409
    assert upload(b"another").status_code == 429


def test_retrieval_routes_share_user_allowance(client, monkeypatch):
    headers = register(client, "audit_search_limit", "searchpass12345")
    monkeypatch.setattr(router_kb, "RETRIEVALS_PER_MINUTE", 1)
    fixed_time = cache.time.time()
    monkeypatch.setattr(cache, "time", SimpleNamespace(time=lambda: fixed_time))
    assert client.post("/api/kb/search", headers=headers, json={"query": "pool", "mode": "bm25"}).status_code == 200
    assert client.post("/api/maintenance/retrieval-test", headers=headers, json={"query": "pool"}).status_code == 429


def test_organization_records_and_platform_evaluation_have_distinct_permissions(client, viewer_headers):
    for path in ("/api/tickets", "/api/tickets/missing", "/api/remediations"):
        assert client.get(path, headers=viewer_headers).status_code == 403
    org_admin = seed_tenant_admin(client, "audit_eval_org", "audit_eval_access", "evalpass12345")
    for path in ("runs", "results", "compare?baseline=x&candidate=y"):
        assert client.get(f"/api/eval/{path}", headers=org_admin).status_code == 403


def test_operations_exclude_private_and_deleted_runs(client, admin_headers, monkeypatch):
    monkeypatch.setattr(get_settings(), "drill_enabled", True)
    monkeypatch.setattr(get_settings(), "remediation_enabled", True)
    private = client.post("/api/support/cases", json={"title": "私人排障"}, headers=admin_headers).json()["id"]
    deleted = new_session(client, admin_headers)
    with SessionLocal() as db:
        db.get(ChatSession, deleted).deleted_at = _now()
        tickets = []
        for session_id in (private, deleted):
            db.add(AgentRun(id=session_id, tenant_id="default", session_id=session_id, mode="rule", status="completed", data={}))
            tickets.append(Ticket(tenant_id="default", session_id=session_id, title="排除测试"))
        db.add_all(tickets)
        db.flush()
        ticket_ids = [ticket.id for ticket in tickets]
        db.commit()
    rejected = client.post("/api/remediations", headers=admin_headers, json={"run_id": private, "reason": "排除测试"})
    assert rejected.status_code == 404
    for ticket_id in ticket_ids:
        detail = client.get(f"/api/maintenance/operations/{ticket_id}", headers=admin_headers)
        assert detail.status_code == 200 and detail.json()["run"] is None
