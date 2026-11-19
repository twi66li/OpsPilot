"""混合检索门面：稠密召回 + BM25 → RRF 融合 → 可选宽召回重排 + 知识版本自动失效缓存。

同时返回各路原始结果，便于不同检索方案的离线对比评测。
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

from app.db.models import Document, DocumentParent, RetrievalLog
from app.rag.embeddings import tokenize
from app.rag.vector_store import RetrievedChunk

logger = logging.getLogger(__name__)


def _serialize_chunk(chunk: RetrievedChunk) -> dict:
    return {
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "doc_title": chunk.doc_title,
        "text": chunk.text,
        "source": chunk.source,
        "score": chunk.score,
        "tenant_id": chunk.tenant_id,
        "extra": chunk.extra,
    }


def _deserialize_chunk(item: dict) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=item["chunk_id"],
        doc_id=item["doc_id"],
        doc_title=item["doc_title"],
        text=item["text"],
        source=item.get("source", ""),
        score=float(item.get("score", 0.0)),
        tenant_id=item.get("tenant_id", ""),
        extra=dict(item.get("extra") or {}),
    )


@dataclass
class RetrievalResult:
    hits: list = field(default_factory=list)
    dense_raw: list = field(default_factory=list)
    bm25_raw: list = field(default_factory=list)
    mode: str = "hybrid"
    latency_ms: float = 0.0
    reranked: bool = False
    cache_hit: bool = False


class HybridRetriever:
    def __init__(self, vector_store, bm25, embedder, reranker=None, settings=None, cache=None):
        self.vector_store = vector_store
        self.bm25 = bm25
        self.embedder = embedder
        self.reranker = reranker
        self.settings = settings
        self.cache = cache
        self.cache_ttl = getattr(settings, "retrieval_cache_ttl", 120)
        self.mode_name = "hybrid"  # 评测脚本可动态切换对比
        self.rrf_k = getattr(settings, "rrf_k", 60)
        self.top_k = getattr(settings, "top_k", 5)
        self.dense_top_k = getattr(settings, "dense_top_k", 5)
        self.bm25_top_k = getattr(settings, "bm25_top_k", 5)

    def _cache_key(self, tenant_id: str, mode: str, top_k: int, query: str, active_versions: dict) -> str:
        reranker_tag = (
            f"{type(self.reranker).__name__}:{getattr(self.reranker, 'model', '')}"
            if self.reranker is not None
            else "none"
        )
        fingerprint_payload = json.dumps(
            {"versions": sorted(active_versions.items()), "query": query},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()[:32]
        return f"ret:v1:{tenant_id}:{mode}:{top_k}:{reranker_tag}:{digest}"

    def retrieve(
        self, db, tenant_id: str, query: str, mode: str = "hybrid", top_k: int | None = None, log: bool = True
    ) -> RetrievalResult:
        top_k = top_k or self.top_k
        start = time.perf_counter()
        result = RetrievalResult(mode=mode)
        active_versions = (
            {
                doc_id: version_id
                for doc_id, version_id in db.query(Document.id, Document.active_version_id)
                .filter_by(tenant_id=tenant_id, deleted_at=None)
                .all()
            }
            if db is not None
            else None
        )

        cache_key = None
        if self.cache is not None and active_versions is not None and self.cache_ttl > 0:
            cache_key = self._cache_key(tenant_id, mode, top_k, query, active_versions)
            try:
                cached_raw = self.cache.get(cache_key)
                if cached_raw:
                    payload = json.loads(cached_raw)
                    result.hits = [_deserialize_chunk(item) for item in payload.get("hits", [])]
                    result.dense_raw = [_deserialize_chunk(item) for item in payload.get("dense_raw", [])]
                    result.bm25_raw = [_deserialize_chunk(item) for item in payload.get("bm25_raw", [])]
                    result.reranked = bool(payload.get("reranked", False))
                    result.cache_hit = True
            except Exception as exc:
                logger.warning("retrieval_cache_read_failed error_type=%s", type(exc).__name__)

        if not result.cache_hit:
            # 开启精排时放大粗召回与 RRF 候选池，避免 RRF 过早截断导致精排失效
            candidate_k = max(top_k * 3, 15) if self.reranker is not None else top_k
            if mode in ("dense", "hybrid"):
                query_vector = self.embedder.embed([query])[0]
                result.dense_raw = self.vector_store.search(
                    tenant_id, query_vector, top_k=max(candidate_k, self.dense_top_k), active_versions=active_versions
                )
            if mode in ("bm25", "hybrid"):
                result.bm25_raw = self.bm25.search(
                    tenant_id, query, top_k=max(candidate_k, self.bm25_top_k), active_versions=active_versions
                )

            if mode == "dense":
                result.hits = result.dense_raw[:candidate_k]
            elif mode == "bm25":
                result.hits = result.bm25_raw[:candidate_k]
            else:
                from app.rag.fusion import rrf_fuse

                result.hits = rrf_fuse([result.dense_raw, result.bm25_raw], k=self.rrf_k, top_k=candidate_k)

            if self.reranker is not None and result.hits:
                result.hits = self.reranker.rerank(query, list(result.hits), top_k=top_k)
                result.reranked = True
            else:
                result.hits = result.hits[:top_k]

            if db is not None and result.hits:
                parent_ids = list({h.extra.get("parent_id") for h in result.hits if h.extra.get("parent_id")})
                if parent_ids:
                    parents = {
                        p.id: p
                        for p in db.query(DocumentParent)
                        .filter(DocumentParent.tenant_id == tenant_id, DocumentParent.id.in_(parent_ids))
                        .all()
                    }
                    for hit in result.hits:
                        parent = parents.get(hit.extra.get("parent_id"))
                        if parent:
                            hit.extra["matched_text"] = hit.text
                            # 大代码/表格父块只提供命中附近的有界窗口，原文仍可按引用回查。
                            window_start = max(0, parent.text.find(hit.text.split("\n", 1)[-1]) - 500)
                            hit.text = parent.text[window_start : window_start + 2400]

            if cache_key is not None:
                try:
                    self.cache.set(
                        cache_key,
                        json.dumps(
                            {
                                "hits": [_serialize_chunk(h) for h in result.hits],
                                "dense_raw": [_serialize_chunk(h) for h in result.dense_raw],
                                "bm25_raw": [_serialize_chunk(h) for h in result.bm25_raw],
                                "reranked": result.reranked,
                            },
                            ensure_ascii=False,
                        ),
                        ttl=self.cache_ttl,
                    )
                except Exception as exc:
                    logger.warning("retrieval_cache_write_failed error_type=%s", type(exc).__name__)

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
