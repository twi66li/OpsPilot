"""知识库接口测试：入库 / 三种模式检索 / 删除 / 租户隔离。"""
import uuid

from tests.conftest import register

DOC_TITLE = "测试连接池手册"


def _ingest_pool_doc(client, admin_headers):
    resp = client.post(
        "/api/kb/documents",
        json={
            "title": DOC_TITLE,
            "content": "# 连接池耗尽处理\n\nERR_5003 表示数据库连接池耗尽。处理步骤：检查 maxPoolSize，清理 Sleep 连接，治理慢 SQL。",
            "source": "test",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_ingest_and_search_all_modes(client, admin_headers):
    doc_id = _ingest_pool_doc(client, admin_headers)
    assert doc_id
    for mode in ("dense", "bm25", "hybrid"):
        resp = client.post("/api/kb/search", json={"query": "ERR_5003 连接池耗尽怎么处理", "mode": mode, "top_k": 3}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        hits = resp.json()["hits"]
        assert hits, f"mode={mode} 应有命中"
        assert any("连接池" in h["doc_title"] for h in hits)


def test_search_invalid_mode(client, admin_headers):
    resp = client.post("/api/kb/search", json={"query": "任意", "mode": "wrong"}, headers=admin_headers)
    assert resp.status_code == 400


def test_list_and_get_document(client, admin_headers):
    doc_id = _ingest_pool_doc(client, admin_headers)
    listed = client.get("/api/kb/documents", headers=admin_headers).json()
    assert any(d["id"] == doc_id for d in listed)
    detail = client.get(f"/api/kb/documents/{doc_id}", headers=admin_headers).json()
    assert "ERR_5003" in detail["content"]


def test_delete_document(client, admin_headers):
    doc_id = _ingest_pool_doc(client, admin_headers)
    assert client.delete(f"/api/kb/documents/{doc_id}", headers=admin_headers).status_code == 200
    assert client.get(f"/api/kb/documents/{doc_id}", headers=admin_headers).status_code == 404


def test_tenant_isolation_kb(client, admin_headers):
    """租户 B 无法检索/列出租户 A 的文档（业务数据与向量检索双重隔离）。"""
    doc_id = _ingest_pool_doc(client, admin_headers)
    tenant_b = uuid.uuid4().hex[:8]
    headers_b = register(client, f"user_{tenant_b}", "pass123456", tenant_id=f"t-{tenant_b}")

    listed = client.get("/api/kb/documents", headers=headers_b).json()
    assert all(d["id"] != doc_id for d in listed)

    resp = client.post("/api/kb/search", json={"query": "ERR_5003 连接池耗尽", "mode": "hybrid"}, headers=headers_b)
    hits = resp.json()["hits"]
    assert all(h["doc_title"] != DOC_TITLE for h in hits)

    assert client.get(f"/api/kb/documents/{doc_id}", headers=headers_b).status_code == 404
