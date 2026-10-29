"""BM25 关键词检索（rank-bm25 + jieba），按租户维护倒排索引。"""
import threading

from rank_bm25 import BM25Okapi

from app.rag.embeddings import tokenize
from app.rag.vector_store import RetrievedChunk


class BM25Retriever:
    def __init__(self):
        self._lock = threading.Lock()
        self._chunks: dict[str, dict[str, dict]] = {}  # tenant -> chunk_id -> chunk
        self._tokens: dict[str, list[list[str]]] = {}  # tenant -> 与 chunk 顺序对齐的分词结果
        self._index: dict[str, BM25Okapi | None] = {}

    def _rebuild(self, tenant_id: str) -> None:
        chunks = list(self._chunks.get(tenant_id, {}).values())
        if chunks:
            tokens = [tokenize(c["text"]) for c in chunks]
            self._tokens[tenant_id] = tokens
            self._index[tenant_id] = BM25Okapi(tokens)
        else:
            self._tokens[tenant_id] = []
            self._index[tenant_id] = None

    def replace_tenant(self, tenant_id: str, chunks: list[dict]) -> None:
        """用全量分块重建某租户的索引（文档删除后使用）。"""
        with self._lock:
            self._chunks[tenant_id] = {c["chunk_id"]: c for c in chunks}
            self._rebuild(tenant_id)

    def add(self, tenant_id: str, chunks: list[dict]) -> None:
        with self._lock:
            bucket = self._chunks.setdefault(tenant_id, {})
            for chunk in chunks:
                bucket[chunk["chunk_id"]] = chunk
            self._rebuild(tenant_id)

    def remove_doc(self, tenant_id: str, doc_id: str) -> int:
        with self._lock:
            bucket = self._chunks.get(tenant_id, {})
            removed = [cid for cid, c in bucket.items() if c.get("doc_id") == doc_id]
            for cid in removed:
                del bucket[cid]
            self._rebuild(tenant_id)
            return len(removed)

    def search(self, tenant_id: str, query: str, top_k: int = 5) -> list[RetrievedChunk]:
        with self._lock:
            index = self._index.get(tenant_id)
            chunks = self._chunks.get(tenant_id, {})
            tokens = self._tokens.get(tenant_id, [])
        if index is None or not chunks:
            return []
        chunk_list = list(chunks.values())
        q_tokens = set(tokenize(query))
        scores = index.get_scores(tokenize(query))
        # 先做词面预过滤：小语料下 rank-bm25 的 idf 为负，未命中文档反而得 0 分会排到前面，
        # 因此仅对与查询共享至少一个词元的文档按 BM25 得分排序。
        candidates = [
            (scores[i], chunk_list[i])
            for i in range(min(len(chunk_list), len(tokens)))
            if q_tokens & set(tokens[i])
        ]
        candidates.sort(key=lambda x: x[0], reverse=True)
        return [
            RetrievedChunk(
                chunk_id=c["chunk_id"],
                doc_id=c["doc_id"],
                doc_title=c["doc_title"],
                text=c["text"],
                source=c.get("source", ""),
                score=float(score),
                tenant_id=tenant_id,
            )
            for score, c in candidates[:top_k]
        ]

    def count(self, tenant_id: str) -> int:
        return len(self._chunks.get(tenant_id, {}))
