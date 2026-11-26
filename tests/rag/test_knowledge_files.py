"""文件入库的关键契约：真实PDF、发布隔离、失败重试和引用权限。"""
import io
import json
import uuid
from zipfile import ZipFile

import httpx
import pytest
from pydantic import SecretStr
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.container import get_container
from app.services.ingestion_service import process_version
from tests.conftest import seed_tenant_admin


def text_pdf(*texts):
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=595, height=842)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 30 700 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.fixture(autouse=True)
def serial_ingestion(monkeypatch, tmp_path):
    container = get_container()
    monkeypatch.setattr(container.settings, "kb_storage_path", str(tmp_path))
    monkeypatch.setattr(container.settings, "mineru_api_key", SecretStr(""))
    monkeypatch.setattr(container.settings, "pdf_parser_mode", "auto")
    monkeypatch.setattr("app.api.router_kb.enqueue", process_version)


def upload(client, headers, content, name="runbook.md", doc_id="", parsed=None):
    files = {"file": (name, content)}
    if parsed is not None:
        files["parsed_file"] = ("content_list.json", json.dumps(parsed).encode())
    response = client.post("/api/kb/documents/upload", headers=headers, files=files, data={"doc_id": doc_id})
    assert response.status_code == 202, response.text
    result = response.json()
    return client.get(f"/api/kb/versions/{result['version_id']}", headers=headers).json()


def test_pdf_source_chunk_and_original(client, admin_headers):
    raw = text_pdf("ERR_PDF_POOL maxPoolSize runbook", "Restart pool after approval")
    version = upload(client, admin_headers, raw, "pool.pdf")
    assert version["status"] == "ready", version
    assert version["parser"] == "pypdf"
    assert [p["page"] for p in version["pages"]] == [1, 2]
    hit = client.post("/api/kb/search", headers=admin_headers,
                      json={"query": "ERR_PDF_POOL", "mode": "bm25"}).json()["hits"][0]
    assert hit["version_id"] == version["version_id"] and hit["page_start"] == 1
    citation = client.get(f"/api/kb/chunks/{hit['chunk_id']}", headers=admin_headers).json()
    assert "maxPoolSize" in citation["parent_text"]
    assert client.get(citation["file_url"], headers=admin_headers).content == raw


def test_version_publication_failure_retry_and_rollback(client, admin_headers, monkeypatch):
    first = upload(client, admin_headers, b"# Pool\n\nOLD_UNIQUE_POOL_ACTION inspect connections")
    container = get_container()
    original_embed = container.embedder.embed
    monkeypatch.setattr(container.embedder, "embed", lambda _: (_ for _ in ()).throw(ValueError("temporary failure")))
    failed = upload(client, admin_headers, b"# Pool\n\nNEW_UNIQUE_POOL_ACTION restart connections", doc_id=first["doc_id"])
    assert failed["status"] == "failed"
    monkeypatch.setattr(container.embedder, "embed", original_embed)
    for mode in ("dense", "bm25"):
        hits = client.post("/api/kb/search", headers=admin_headers,
                           json={"query": "UNIQUE_POOL_ACTION", "mode": mode, "top_k": 20}).json()["hits"]
        assert any(h.get("version_id") == first["version_id"] for h in hits)
        assert not any(h.get("version_id") == failed["version_id"] for h in hits)
    assert client.post(f"/api/kb/versions/{failed['id']}/retry", headers=admin_headers).status_code == 202
    latest = client.get(f"/api/kb/versions/{failed['id']}", headers=admin_headers).json()
    assert latest["status"] == "ready" and latest["active"]
    for mode in ("dense", "bm25"):
        hits = client.post("/api/kb/search", headers=admin_headers,
                           json={"query": "UNIQUE_POOL_ACTION", "mode": mode, "top_k": 20}).json()["hits"]
        assert any(h.get("version_id") == failed["id"] for h in hits)
        assert not any(h.get("version_id") == first["id"] for h in hits)
    assert client.post(f"/api/kb/versions/{first['id']}/activate", headers=admin_headers).json()["active"]
    again = upload(client, admin_headers, b"# Pool\n\nOLD_UNIQUE_POOL_ACTION inspect connections", doc_id=first["doc_id"])
    assert again["id"] == first["id"]


def test_mineru_structure_and_tenant_permissions(client, admin_headers, viewer_headers):
    version = upload(client, admin_headers, text_pdf("original source"), "ops.pdf", parsed=[
        {"type": "text", "text": "故障处理", "text_level": 1, "page_idx": 0},
        {"type": "text", "text": "先核对审批记录，再重启演练服务。", "page_idx": 0},
        {"type": "table", "table_body": "<table><tr><th>指标</th><th>阈值</th></tr><tr><td>错误率</td><td>5%</td></tr></table>", "page_idx": 0},
        {"type": "code", "code_body": "curl http://demo/health", "code_language": "bash", "page_idx": 0},
    ])
    assert version["status"] == "ready", version
    assert {c["content_type"] for c in version["chunks"]} >= {"code", "table"}
    assert "| 指标 | 阈值 |" in version["pages"][0]["text"]
    other = seed_tenant_admin(client, uuid.uuid4().hex, "kb_" + uuid.uuid4().hex[:8], "pass123456")
    for path in (f"versions/{version['id']}", f"versions/{version['id']}/file", f"chunks/{version['chunks'][0]['chunk_id']}"):
        assert client.get("/api/kb/" + path, headers=other).status_code == 404
    assert client.post("/api/kb/documents/upload", headers=viewer_headers, files={"file": ("x.txt", b"x")}).status_code == 403


def test_bad_pdf_is_visible_failed_task(client, admin_headers):
    version = upload(client, admin_headers, b"not a pdf", "bad.pdf")
    assert version["status"] == "failed" and not version["active"] and version["error"]


@pytest.mark.parametrize("failure", [None, "failed", "timeout"])
def test_mineru_online_upload_and_index(client, admin_headers, monkeypatch, failure):
    settings = get_container().settings
    monkeypatch.setattr(settings, "mineru_api_key", SecretStr("mineru-test-token"))
    raw = text_pdf(f"First source page: {failure or 'success'}", "Second source page")
    content_list = [
        {"type": "text", "text": "MINERU_ONLINE_PAGE_ONE", "page_idx": 0},
        {"type": "text", "text": "MINERU_ONLINE_PAGE_TWO", "page_idx": 1},
    ]
    archive = io.BytesIO()
    with ZipFile(archive, "w") as result:
        result.writestr("nested/document_content_list.json", json.dumps(content_list))
    steps = []

    def respond(request):
        steps.append((request.method, request.url.path))
        if request.url.host == "mineru.net":
            assert request.headers["authorization"] == "Bearer mineru-test-token"
            if request.method == "POST":
                assert json.loads(request.content)["files"] == [{"name": "document.pdf"}]
                data = {"batch_id": "batch", "file_urls": ["https://uploads.example.test/document"]}
            else:
                if failure == "timeout":
                    raise httpx.ReadTimeout("https://example.test/?signed=secret", request=request)
                state = "failed" if failure else "running" if len(steps) == 3 else "done"
                data = {"extract_result": [{"state": state, "full_zip_url": "https://results.example.test/result"}]}
            return httpx.Response(200, json={"code": 0, "data": data})
        assert "authorization" not in request.headers
        if request.method == "PUT":
            assert request.content == raw
            assert "content-type" not in request.headers
            return httpx.Response(200)
        return httpx.Response(200, content=archive.getvalue())

    monkeypatch.setattr("app.services.mineru.Client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr("app.services.mineru.sleep", lambda _: None)
    version = upload(client, admin_headers, raw, "online.pdf")
    assert steps[:3] == [
        ("POST", "/api/v4/file-urls/batch"), ("PUT", "/document"), ("GET", "/api/v4/extract-results/batch/batch")
    ]
    if failure:
        assert version["status"] == "failed" and not version["active"]
        assert ("超时" if failure == "timeout" else "解析 PDF 失败") in version["error"]
        assert "signed" not in version["error"] and "mineru-test-token" not in version["error"]
        return
    assert steps[-2:] == [("GET", "/api/v4/extract-results/batch/batch"), ("GET", "/result")]
    assert version["status"] == "ready" and version["parser"] == "mineru-content-list"
    assert [p["page"] for p in version["pages"]] == [1, 2]
    hits = client.post("/api/kb/search", headers=admin_headers,
                       json={"query": "MINERU_ONLINE_PAGE_TWO", "mode": "dense"}).json()["hits"]
    assert any(hit["version_id"] == version["version_id"] for hit in hits)


def test_qdrant_filters_versions_before_topk():
    from app.rag.vector_store import QdrantVectorStore

    store = QdrantVectorStore(mode="memory", dim=2)
    store.upsert("t", [{"chunk_id": f"c{i}", "doc_id": "d", "doc_title": "d", "text": str(i), "version_id": f"v{i}"}
                       for i in range(2)], [[1.0, 0.0], [0.5, 0.5]])
    hits = store.search("t", [1.0, 0.0], top_k=1, active_versions={"d": "v1"})
    assert len(hits) == 1 and hits[0].extra["version_id"] == "v1"
