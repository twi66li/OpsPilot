import json
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from app.db.base import SessionLocal
from app.db.models import AgentRun, ChatSession, Document, DocumentVersion, SupportCase
from app.services.evidence_checks import summarize_evidence
from app.services.maintenance_service import collection_guide
from app.services.scenario_rules import BY_COLLECTION_KEY, SCENARIOS, match_scenarios
from app.services.support_service import redact
from tests.conftest import register


def test_shared_scenarios_drive_collection_and_matching():
    assert set(BY_COLLECTION_KEY) == {"pool", "nginx", "container"}
    for scenario in SCENARIOS:
        guide = collection_guide(scenario["collection_key"], "docker", "app")
        assert guide[0]["scenario_notes"] == scenario["collection_notes"]
        found = match_scenarios(scenario["sample_before"])
        assert scenario["id"] in {item["id"] for item in found}
    assert not match_scenarios('2026-11-15T10:00:00Z GET /health HTTP 200\n{"Status":"running","OOMKilled":false}')


def test_log_summary_counts_only_submitted_lines_and_preserves_anchors():
    text = ('2026-11-15T10:00:00Z GET /orders?token=hidden HTTP 502\n'
            '2026-11-15T10:00:01Z GET /orders HTTP 502\n'
            'sqlalchemy.exc.TimeoutError: QueuePool limit reached\n'
            'sqlalchemy.exc.TimeoutError: QueuePool limit reached')
    summary = summarize_evidence({"id": "E1", "kind": "initial", "text": text})
    assert summary["total_lines"] == 4
    assert summary["http_statuses"] == [{"status": 502, "count": 2, "lines": [1, 2]}]
    assert summary["endpoints"][0]["endpoint"] == "/orders"
    assert summary["exceptions"] == [{"name": "TimeoutError", "count": 2, "lines": [3, 4]}]
    assert summary["repeated"] == [{"count": 2, "lines": [3, 4]}]
    assert summary["time_range"]["recognized"] == 2
    assert "hidden" not in json.dumps(summary)
    empty = summarize_evidence({"id": "E2", "kind": "initial", "text": "没有提供请求结果"})
    assert empty["time_range"] is None and empty["http_statuses"] == []
    assert "不代表正常" in empty["notice"]


def test_redaction_reports_categories_without_echoing_secrets_and_is_idempotent():
    text = ('{"accessToken":"sensitive-one","client_secret":"sensitive-two"}\n'
            'postgresql://app:sensitive-three@10.0.0.8/db\nuser@example.org\n'
            '-----BEGIN PRIVATE KEY-----\nsensitive-key-data\n-----END PRIVATE KEY-----\n'
            '10.0.0.8 sqlalchemy.exc.TimeoutError: QueuePool limit reached')
    result = redact(text)
    for secret in ["sensitive-one", "sensitive-two", "sensitive-three", "sensitive-key-data", "user@example.org"]:
        assert secret not in json.dumps(result, ensure_ascii=False)
    assert result["text"].count("\n") == text.count("\n")
    assert "10.0.0.8" in result["text"] and result["review_findings"][0]["count"] == 2
    assert {item["label"] for item in result["findings"]} >= {"私钥", "口令或密钥", "连接串密码", "邮箱"}
    again = redact(result["text"])
    assert again["text"] == result["text"] and again["replacements"] == 0


def test_todos_sort_urgent_cases_and_exclude_closed_or_other_users(client, admin_headers):
    headers = register(client, "remaining_priority", "pass123456")
    ids = []
    for title in ["普通待办", "紧急待办", "已归档待办", "已解决待办"]:
        ids.append(client.post('/api/support/cases', headers=headers, json={"title": title}).json()["id"])
    with SessionLocal() as db:
        db.get(SupportCase, ids[0]).severity = "P2"
        db.get(SupportCase, ids[1]).severity = "P0"
        row = db.get(SupportCase, ids[2])
        row.data = {**row.data, "archived": True}
        db.get(SupportCase, ids[3]).status = "user_resolved"
        db.commit()
    items = client.get('/api/maintenance/todos', headers=headers).json()["items"]
    assert items[0]["resource_id"] == ids[1]
    assert {item["resource_id"] for item in items} == set(ids[:2])
    other = client.get('/api/maintenance/todos', headers=admin_headers).json()["items"]
    assert not any(item.get("resource_id") in ids for item in other)


def test_preview_and_saved_evidence_both_expose_safe_summary(client, admin_headers):
    body = {"text": "2026-11-15T10:00:00Z GET /orders HTTP 502\npassword=private-value", "label": "日志"}
    preview = client.post('/api/support/evidence/preview', headers=admin_headers, json=body).json()
    assert preview["replacements"] == 1 and preview["summary"]["request_count"] == 1
    record = client.post('/api/support/cases', headers=admin_headers, json={"title": "摘要集成"}).json()
    assert client.post(f'/api/support/cases/{record["id"]}/evidence', headers=admin_headers, json=body).status_code == 200
    saved = client.get(f'/api/support/cases/{record["id"]}', headers=admin_headers).json()["evidence"][0]
    assert saved["summary"]["http_statuses"][0]["lines"] == [1]
    assert "private-value" not in json.dumps(saved)


def test_interrupted_todo_prefills_existing_case_without_running_and_expires(client):
    headers = register(client, "remaining_resume", "pass123456")
    record = client.post('/api/support/cases', headers=headers, json={"title": "保留现场证据"}).json()
    sid = record['id']
    client.post(f'/api/support/cases/{sid}/evidence', headers=headers, json={"text": "HTTP 502", "label": "原日志"})
    with SessionLocal() as db:
        case = db.get(SupportCase, sid)
        created = datetime(2026, 11, 15)
        db.add(AgentRun(id='remaining-interrupted', session_id=sid, tenant_id=case.tenant_id, mode='rule', status='interrupted', created_at=created, data={'question': '用原有证据分析 & 核对'}))
        db.commit()
    items = client.get('/api/maintenance/todos', headers=headers).json()['items']
    todo = next(item for item in items if item['kind'] == 'run')
    url = urlparse(todo['url'])
    assert url.path == f'/support/{sid}' and parse_qs(url.query)['question'] == ['用原有证据分析 & 核对']
    assert todo['action'] == 'resume_analysis'
    assert len(client.get(f'/api/support/cases/{sid}', headers=headers).json()['evidence']) == 1
    with SessionLocal() as db:
        db.add(AgentRun(id='remaining-completed', session_id=sid, tenant_id=db.get(SupportCase, sid).tenant_id, mode='rule', status='completed', created_at=created+timedelta(seconds=1), data={}))
        db.commit()
    assert not any(item['id'] == todo['id'] for item in client.get('/api/maintenance/todos', headers=headers).json()['items'])
    with SessionLocal() as db:
        row = db.get(SupportCase, sid)
        row.data = {**row.data, 'archived': True}
        db.get(AgentRun, 'remaining-completed').status = 'interrupted'
        db.commit()
    assert not any(item['kind'] == 'run' for item in client.get('/api/maintenance/todos', headers=headers).json()['items'])


def test_failed_document_todo_uses_latest_version_and_retry_clears_it(client, monkeypatch):
    from app.services.knowledge_access import personal_namespace

    headers = register(client, 'remaining_document', 'pass123456')
    user = client.get('/auth/me', headers=headers).json()
    # 队列行为独立验证，不在这个用例启动解析线程。
    monkeypatch.setattr('app.api.router_kb.enqueue', lambda *args: None)
    with SessionLocal() as db:
        session = client.post('/api/sessions', headers=headers, json={'title': '定位本人'}).json()
        owner = db.get(ChatSession, session['id']).user_id
        namespace = personal_namespace(owner)
        assert user['role'] == 'viewer'
        db.add(Document(id='remaining-doc', tenant_id=namespace, created_by=owner, title='待重试手册', content='原文'))
        db.flush()
        for number, state in [(1, 'failed'), (2, 'ready')]:
            db.add(DocumentVersion(id=f'remaining-v{number}', tenant_id=namespace, doc_id='remaining-doc', number=number, filename='manual.txt', content_hash='hash', original_path='unused', status=state))
        db.commit()
    def documents():
        return [item for item in client.get('/api/maintenance/todos', headers=headers).json()['items'] if item['kind'] == 'document']
    assert not documents()
    with SessionLocal() as db:
        db.get(DocumentVersion, 'remaining-v2').status = 'failed'
        db.commit()
    assert [item['id'] for item in documents()] == ['doc-remaining-v2']
    response = client.post('/api/maintenance/documents/retry-failed', headers=headers, json={'ids': ['remaining-doc']})
    assert response.json()['items'][0]['status'] == 'queued'
    assert not documents()
