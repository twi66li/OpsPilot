"""RAG 组件单元测试：分块 / 向量 / BM25 / RRF / 重排 / 评测指标。"""
from app.rag.bm25 import BM25Retriever
from app.rag.chunking import split_into_chunks
from app.rag.embeddings import MockEmbedding, tokenize
from app.rag.fusion import rrf_fuse
from app.rag.rerank import HeuristicReranker
from app.rag.vector_store import InMemoryVectorStore, RetrievedChunk


def make_chunk(cid: str, text: str) -> dict:
    return {"chunk_id": cid, "doc_id": f"doc-{cid}", "doc_title": f"标题{cid}", "text": text, "source": "", "chunk_index": 0}


def make_hit(cid: str, text: str) -> RetrievedChunk:
    return RetrievedChunk(chunk_id=cid, doc_id=f"doc-{cid}", doc_title=f"标题{cid}", text=text)


def test_tokenize_keeps_error_code_and_service():
    tokens = tokenize("ERR_5003 order-service 连接池耗尽")
    assert "err_5003" in tokens
    assert "order-service" in tokens
    assert "连接池" in "".join(tokens) or "连接" in tokens


def test_chunking_long_doc():
    content = "# 标题\n" + "这是一段很长的内容。" * 200
    chunks = split_into_chunks(content, chunk_size=500, overlap=80)
    assert len(chunks) > 1
    assert all(len(c) <= 520 for c in chunks)


def test_chunking_empty():
    assert split_into_chunks("") == []


def test_mock_embedding_deterministic_and_normalized():
    emb = MockEmbedding(dim=128)
    v1 = emb.embed(["连接池耗尽处理"])[0]
    v2 = emb.embed(["连接池耗尽处理"])[0]
    assert v1 == v2
    assert abs(sum(x * x for x in v1) - 1.0) < 1e-6
    assert len(v1) == 128


def test_mock_embedding_similarity_by_lexical_overlap():
    emb = MockEmbedding(dim=128)
    q = emb.embed(["ERR_5003 连接池耗尽"])[0]
    near = emb.embed(["ERR_5003 是连接池耗尽的错误码"])[0]
    far = emb.embed(["今天天气不错适合出去走走"])[0]

    def dot(a, b):
        return sum(x * y for x, y in zip(a, b))

    assert dot(q, near) > dot(q, far)


def test_inmemory_vector_store_tenant_isolation():
    store = InMemoryVectorStore(dim=64)
    emb = MockEmbedding(dim=64)
    chunks = [make_chunk("c1", "连接池耗尽处理手册")]
    store.upsert("tenant-a", chunks, emb.embed([c["text"] for c in chunks]))
    q = emb.embed(["连接池耗尽"])[0]
    assert len(store.search("tenant-a", q, top_k=5)) == 1
    assert store.search("tenant-b", q, top_k=5) == []
    assert store.delete_doc("tenant-a", "doc-c1") == 1
    assert store.search("tenant-a", q, top_k=5) == []


def test_bm25_add_search_remove():
    bm25 = BM25Retriever()
    bm25.add("t1", [make_chunk("a", "数据库连接池耗尽处理"), make_chunk("b", "Kafka 消费积压处理")])
    hits = bm25.search("t1", "连接池耗尽", top_k=2)
    assert hits and hits[0].chunk_id == "a"
    assert bm25.search("t2", "连接池", top_k=2) == []
    assert bm25.remove_doc("t1", "doc-a") == 1
    assert bm25.search("t1", "连接池耗尽", top_k=2) == []


def test_rrf_fuse_prefers_consensus():
    a = [make_hit("y", "y"), make_hit("x", "x"), make_hit("z", "z")]
    b = [make_hit("y", "y"), make_hit("z", "z"), make_hit("w", "w")]
    fused = rrf_fuse([a, b], k=60, top_k=3)
    # y 在两路均为第一，RRF 得分最高；z 两路均上榜次之；x 仅一路上榜
    assert [c.chunk_id for c in fused] == ["y", "z", "x"]


def test_heuristic_rerank_boosts_overlap():
    reranker = HeuristicReranker()
    hits = [make_hit("irrelevant", "今天天气不错"), make_hit("relevant", "ERR_5003 连接池耗尽处理手册")]
    reranked = reranker.rerank("ERR_5003 连接池耗尽怎么处理", hits, top_k=2)
    assert reranked[0].chunk_id == "relevant"


def test_metrics_recall_mrr():
    from eval.metrics import mrr_at_k, recall_at_k

    assert recall_at_k(["A"], ["A", "B"], 5) == 1.0
    assert recall_at_k(["A", "C"], ["A", "B"], 5) == 0.5
    assert recall_at_k([], ["A"], 5) == 1.0
    assert mrr_at_k(["B"], ["A", "B"], 5) == 0.5
    assert mrr_at_k(["Z"], ["A", "B"], 5) == 0.0


def test_metrics_keyword_coverage_and_faithfulness():
    from eval.metrics import faithfulness_heuristic, keyword_coverage

    assert keyword_coverage("先检查连接池配置", ["连接池", "KILL"]) == 0.5
    assert keyword_coverage("任意回答", []) is None
    ctx = ["数据库连接池耗尽需要清理 Sleep 连接"]
    answer = "连接池耗尽时建议清理 Sleep 连接。今天天气不错。"
    score = faithfulness_heuristic(answer, ctx)
    assert 0.0 < score < 1.0
