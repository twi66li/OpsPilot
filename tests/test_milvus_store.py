"""Milvus 的授权边界与故障行为；真实 SDK/服务器检查另见 integration。"""

from unittest.mock import Mock

import pytest

from app.config import Settings
from app.rag.milvus_store import MilvusVectorStore, _literal, _scope
from app.rag.vector_store import get_vector_store


@pytest.fixture
def store():
    result = object.__new__(MilvusVectorStore)
    result.client, result.collection, result.dim = Mock(), "isolated", 3
    return result


def test_empty_authorization_does_not_query_all(store):
    assert store.search("personal:alice", [1, 0, 0], active_versions={}) == []
    store.client.search.assert_not_called()


def test_scope_pairs_document_and_version_and_quotes_values():
    malicious = 'x" or tenant_id != "x'
    scope = _scope(malicious, {"selected": "v1", "legacy": None})
    assert scope == f'tenant_id == {_literal(malicious)} and ((doc_id == "selected" and version_id == "v1") or (doc_id == "legacy" and version_id == ""))'
    assert '\\"' in scope


def test_payload_locations_and_cosine_scores_survive_search(store):
    payload = {"chunk_id": "c", "doc_id": "d", "doc_title": "手册", "text": "正文", "version_id": "v1",
               "page_start": 4, "parent_id": "p", "heading_path": ["连接池"]}
    store.client.search.return_value = [[{"entity": {"payload": payload}, "distance": 0.75}]]
    hit = store.search("official", [1, 0, 0], active_versions={"d": "v1"})[0]
    assert hit.tenant_id == "official" and hit.score == 0.75
    assert hit.extra == {"version_id": "v1", "page_start": 4, "parent_id": "p", "heading_path": ["连接池"]}
    assert store.client.search.call_args.kwargs["consistency_level"] == "Strong"


def test_upsert_rejects_truncated_batches_and_preserves_full_payload(store):
    chunk = {"chunk_id": "c", "doc_id": "d", "text": "正文", "custom": {"kept": True}}
    with pytest.raises(ValueError):
        store.upsert("alice", [chunk], [])
    with pytest.raises(ValueError):
        store.upsert("alice", [chunk], [[1, 0]])
    store.client.upsert.assert_not_called()
    store.upsert("alice", [chunk], [[1, 0, 0]])
    assert store.client.upsert.call_args.kwargs["data"][0]["payload"] == {**chunk, "tenant_id": "alice"}


def test_scroll_closes_iterator_on_failure(store):
    iterator = store.client.query_iterator.return_value
    iterator.next.side_effect = RuntimeError("query failed")
    with pytest.raises(RuntimeError):
        store.scroll_tenant("alice")
    iterator.close.assert_called_once()
    assert store.client.query_iterator.call_args.kwargs["filter"] == 'tenant_id == "alice"'


def test_milvus_factory_never_falls_back_to_memory(monkeypatch):
    monkeypatch.setattr("app.rag.milvus_store.MilvusVectorStore", Mock(side_effect=ConnectionError("unavailable")))
    with pytest.raises(ConnectionError):
        get_vector_store(Settings(_env_file=None, vector_store_backend="milvus", model_strict=False))


def test_failed_vector_query_makes_readiness_fail(client, monkeypatch):
    from app.container import get_container

    monkeypatch.setattr(get_container().vector_store, "health", Mock(side_effect=ConnectionError("unavailable")))
    assert client.get("/ready").status_code == 503
    assert client.get("/live").status_code == 200


def test_production_milvus_does_not_require_unused_qdrant():
    settings = Settings(_env_file=None, app_env="production", jwt_secret="x" * 40,
                        admin_password="long-unique-password", database_url="postgresql+psycopg://db/opspilot",
                        vector_store_backend="milvus", milvus_uri="http://milvus:19530",
                        llm_provider="openai", embedding_provider="openai")
    assert settings.vector_store_backend == "milvus"
