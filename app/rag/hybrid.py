"""混合检索门面：dense(Qdrant) + BM25 → RRF 融合 → 可选重排。

同时返回各路原始结果，便于不同检索方案的离线对比评测。
"""
import time
from dataclasses import dataclass, field

from app.db.models import RetrievalLog
from app.rag.embeddings import tokenize


@dataclass
class RetrievalResult:
    hits: list = field(default_factory=list)
    dense_raw: list = field(default_factory=list)
    bm25_raw: list = field(default_factory=list)
    mode: str = "hybrid"
    latency_ms: float = 0.0
    reranked: bool = False


class HybridRetriever:
    def __init__(self, vector_store, bm25, embedder, reranker=None, settings=None):
        self.vector_store = vector_store
        self.bm25 = bm25
        self.embedder = embedder
        self.reranker = reranker
        self.settings = settings
        self.mode_name = "hybrid"  # 评测脚本可动态切换对比
        self.rrf_k = getattr(settings, "rrf_k", 60)
        self.top_k = getattr(settings, "top_k", 5)
        self.dense_top_k = getattr(settings, "dense_top_k", 5)
        self.bm25_top_k = getattr(settings, "bm25_top_k", 5)

    def retrieve(self, db, tenant_id: str, query: str, mode: str = "hybrid", top_k: int | None = None, log: bool = True) -> RetrievalResult:
        top_k = top_k or self.top_k
        start = time.perf_counter()
        result = RetrievalResult(mode=mode)

        if mode in ("dense", "hybrid"):
            query_vector = self.embedder.embed([query])[0]
            result.dense_raw = self.vector_store.search(tenant_id, query_vector, top_k=max(top_k, self.dense_top_k))
        if mode in ("bm25", "hybrid"):
            result.bm25_raw = self.bm25.search(tenant_id, query, top_k=max(top_k, self.bm25_top_k))

        if mode == "dense":
            result.hits = result.dense_raw[:top_k]
        elif mode == "bm25":
            result.hits = result.bm25_raw[:top_k]
        else:
            from app.rag.fusion import rrf_fuse

            result.hits = rrf_fuse([result.dense_raw, result.bm25_raw], k=self.rrf_k, top_k=top_k)

        if self.reranker is not None and result.hits:
            result.hits = self.reranker.rerank(query, list(result.hits), top_k=top_k)
            result.reranked = True

        result.latency_ms = (time.perf_counter() - start) * 1000

        if log and db is not None:
            db.add(
                RetrievalLog(
                    tenant_id=tenant_id,
                    query=query,
                    mode=mode,
                    top_k=top_k,
                    hit_doc_ids=[h.doc_id for h in result.hits],
                    scores=[round(h.score, 4) for h in result.hits],
                    latency_ms=result.latency_ms,
                )
            )
            db.commit()
        return result


def token_overlap(query: str, text: str) -> float:
    """评测用：查询与文本的词面重合率。"""
    q = set(tokenize(query))
    if not q:
        return 0.0
    t = set(tokenize(text))
    return len(q & t) / len(q)
