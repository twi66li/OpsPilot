import httpx
import pytest

from app.container import get_container
from app.db.base import SessionLocal
from app.db.models import Document, Tenant
from app.services import http_probe, kb_service
from app.services.evaluation_compare import compare_reports
from app.services.knowledge_access import personal_namespace


def test_http_probe_pins_public_ip_and_never_follows_redirects(monkeypatch):
    monkeypatch.setattr(http_probe.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 80))])
    with pytest.raises(ValueError):
        http_probe.probe_url("http://example.com")
    monkeypatch.setattr(
        http_probe.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))]
    )
    observed = []

    def respond(request):
        observed.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})

    original = httpx.Client
    monkeypatch.setattr(
        http_probe.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs)
    )
    result = http_probe.probe_url("https://example.com/health")
    assert result["state"] == "unknown" and result["http_status"] == 302
    assert len(observed) == 1 and observed[0].url.host == "93.184.216.34"
    assert observed[0].headers["host"] == "example.com" and observed[0].extensions["sni_hostname"] == "example.com"


def test_evaluation_comparison_requires_same_question_and_common_checks():
    old = {
        "id": "old",
        "questions": [{"id": "a", "question": "相同输入", "program_checks": {"source": True, "scope": True}}],
    }
    new = {
        "id": "new",
        "questions": [{"id": "changed-id", "question": "相同输入", "program_checks": {"source": False}}],
    }
    report = compare_reports(old, new)
    assert report["common_questions"] == 1 and report["regressions"] == 1 and report["missing_check_questions"] == 1
    assert report["gate"] == "needs_review"
    new["questions"][0]["question"] = "不同输入"
    assert compare_reports(old, new)["gate"] == "inconclusive"


def test_personal_document_delete_excludes_search_but_retains_source(client, admin_headers, viewer_headers):
    user_id = client.get("/auth/me", headers=admin_headers).json()["user_id"]
    namespace = personal_namespace(user_id)
    with SessionLocal() as db:
        if not db.get(Tenant, namespace):
            db.add(Tenant(id=namespace, name="私人检索验证"))
            db.commit()
        doc = Document(
            tenant_id=namespace,
            title="保留引用的测试手册",
            source="retained.txt",
            content="检查数据库连接池和会话关闭路径",
            created_by=user_id,
        )
        kb_service.ingest_document(db, get_container(), doc)
        doc_id = doc.id
    query = {"query": "数据库连接池", "doc_ids": [doc_id], "include_official": False}
    response = client.post("/api/maintenance/retrieval-test", json=query, headers=admin_headers)
    assert response.status_code == 200 and response.json()["hits"]
    assert client.delete(f"/api/support/documents/{doc_id}", headers=viewer_headers).status_code == 404
    assert client.delete(f"/api/support/documents/{doc_id}", headers=admin_headers).status_code == 200
    assert doc_id not in {d["id"] for d in client.get("/api/support/private-documents", headers=admin_headers).json()}
    assert client.post("/api/maintenance/retrieval-test", json=query, headers=admin_headers).status_code == 400
    with SessionLocal() as db:
        retained = db.get(Document, doc_id)
        assert retained.deleted_at and "会话关闭" in retained.content


def test_frontend_deep_links_fall_back_without_hiding_api_404(client):
    page = client.get("/operations/test-approval-id")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert '<div id="root"></div>' in page.text
    assert client.get("/api/not-a-route").status_code == 404
