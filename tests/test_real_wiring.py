"""接线测试：真实模型/存储链路的装配语义（全部 Mock/假端点，不打真实 API）。"""
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.config import Settings
from app.rag.rerank import DashScopeReranker, get_reranker
from app.rag.vector_store import InMemoryVectorStore, RetrievedChunk, get_vector_store
from app.services import cache as cache_mod

# 真实客户端构造现要求已配置预算账本（T18 fail-closed）；接线测试统一指向临时账本
_BUDGET_DIR = tempfile.mkdtemp(prefix="t7_wiring_budget_")


def _settings(**kw) -> Settings:
    base = dict(
        database_url="sqlite:///:memory:",
        vector_store_backend="milvus",
        milvus_uri="http://127.0.0.1:1",
        embedding_provider="mock",
        embedding_dim=1024,
        llm_provider="mock",
        model_strict=True,
        rerank_enabled=True,
        rerank_provider="dashscope",
        rerank_model="qwen3.7-text-rerank",
        openai_api_key="test-key",
        openai_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        redis_url="redis://127.0.0.1:1/0",
        budget_ledger_path=os.path.join(_BUDGET_DIR, "ledger.jsonl"),
        budget_token_limit=10**9,
        budget_model_limits={"qwen3.7-text-rerank": 10**9},
    )
    base.update(kw)
    return Settings(**base)


@pytest.mark.parametrize("strict", [True, False])
def test_milvus_connection_failure_does_not_fall_back(monkeypatch, strict):
    monkeypatch.setattr("app.rag.milvus_store.MilvusVectorStore", Mock(side_effect=ConnectionError("unavailable")))
    with pytest.raises(ConnectionError):
        get_vector_store(_settings(model_strict=strict))


def test_explicit_offline_vector_store_uses_memory():
    store = get_vector_store(_settings(vector_store_backend="memory"))
    assert isinstance(store, InMemoryVectorStore)


def test_dashscope_reranker_wiring(monkeypatch):
    monkeypatch.setattr("app.config.get_settings", _settings)  # RerankAPI 构造读取全局预算配置
    reranker = get_reranker(_settings())
    assert isinstance(reranker, DashScopeReranker)
    assert reranker.model == "qwen3.7-text-rerank"
    with pytest.raises(ValueError):  # dashscope 路径缺密钥是配置错误，直接拒绝
        get_reranker(_settings(openai_api_key=""))


def test_dashscope_reranker_uses_api_and_no_fallback(monkeypatch):
    monkeypatch.setattr("app.config.get_settings", _settings)

    def fake_ok(self, query, chunks, top_k=5):
        for c in chunks:
            c.score = 1.0
        return list(reversed(chunks))[:top_k]

    monkeypatch.setattr("app.services.model_api.RerankAPI.rerank", fake_ok)
    reranker = get_reranker(_settings())
    chunks = [RetrievedChunk(chunk_id=str(i), doc_id="d", doc_title="t", text=f"chunk{i}") for i in range(3)]
    out = reranker.rerank("查询", chunks, top_k=2)
    assert len(out) == 2 and out[0].chunk_id == "2"

    def boom(self, query, chunks, top_k=5):
        raise RuntimeError("rerank api down")

    monkeypatch.setattr("app.services.model_api.RerankAPI.rerank", boom)
    with pytest.raises(RuntimeError):  # API 失败不回退 heuristic
        reranker.rerank("查询", chunks, top_k=2)


def test_strict_cache_raises_when_redis_down(monkeypatch):
    monkeypatch.setattr(cache_mod, "_cache_instance", None)
    try:
        with pytest.raises(RuntimeError):
            cache_mod.get_cache(_settings())
    finally:
        monkeypatch.setattr(cache_mod, "_cache_instance", None)


def test_non_strict_cache_falls_back(monkeypatch):
    monkeypatch.setattr(cache_mod, "_cache_instance", None)
    try:
        instance = cache_mod.get_cache(_settings(model_strict=False))
        assert isinstance(instance, cache_mod.InMemoryCache)
    finally:
        monkeypatch.setattr(cache_mod, "_cache_instance", None)


def test_strict_embedding_requires_key():
    from app.rag.embeddings import get_embedding_provider

    with pytest.raises(ValueError):
        get_embedding_provider(_settings(embedding_provider="openai", openai_api_key="", model_strict=True))


def test_default_compose_uses_the_local_postgres_stack():
    import yaml

    default = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    local = yaml.safe_load(Path("compose.local.yml").read_text(encoding="utf-8"))
    assert default["include"] == ["compose.local.yml"]
    assert default["name"] == local["name"]
    assert "mysql" not in local["services"]
    api = local["services"]["api"]
    env = api["environment"]
    assert api["env_file"] == ".env"
    assert env["DATABASE_URL"].startswith("postgresql+psycopg://")
    assert env["VECTOR_STORE_BACKEND"] == "milvus"
