"""T7 接线测试：真实模型/存储链路的装配语义（全部 Mock/假端点，不打真实 API）。"""
import pytest

from app.config import Settings
from app.rag.rerank import DashScopeReranker, get_reranker
from app.rag.vector_store import InMemoryVectorStore, RetrievedChunk, get_vector_store
from app.services import cache as cache_mod


def _settings(**kw) -> Settings:
    base = dict(
        database_url="sqlite:///:memory:",
        qdrant_mode="remote",
        qdrant_url="http://127.0.0.1:1",
        qdrant_collection="kb_qwen37_1024_v2",
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
    )
    base.update(kw)
    return Settings(**base)


def test_strict_vector_store_does_not_fall_back():
    with pytest.raises(Exception):  # 连接不可达时 strict 必须暴露，不得退化为进程内
        get_vector_store(_settings())


def test_non_strict_vector_store_falls_back_to_memory():
    store = get_vector_store(_settings(model_strict=False))
    assert isinstance(store, InMemoryVectorStore)


def test_dashscope_reranker_wiring():
    reranker = get_reranker(_settings())
    assert isinstance(reranker, DashScopeReranker)
    assert reranker.model == "qwen3.7-text-rerank"
    with pytest.raises(ValueError):  # dashscope 路径缺密钥是配置错误，直接拒绝
        get_reranker(_settings(openai_api_key=""))


def test_dashscope_reranker_uses_api_and_no_fallback(monkeypatch):
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


def test_compose_carries_t7_passthrough():
    import yaml

    doc = yaml.safe_load(open("docker-compose.yml", encoding="utf-8"))
    env = doc["services"]["api"]["environment"]
    text = "\n".join(f"{k}: {v}" for k, v in env.items())
    for token in (
        "kb_qwen37_1024_v2",
        "qwen3.7-text-embedding-flash",
        "qwen3.7-text-rerank",
        "MODEL_STRICT",
        "dashscope.aliyuncs.com",
    ):
        assert token in text, token
    assert env["QDRANT_COLLECTION"].endswith("kb_qwen37_1024_v2}")
    assert env["EMBEDDING_DIM"].endswith("1024}")
    assert env["CHAT_MODEL"] == "${CHAT_MODEL:-qwen3.8-max}"
