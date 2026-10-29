"""向量库抽象：Qdrant（生产）与进程内实现（开发/测试离线退化）。

统一接口，检索时强制 tenant 过滤：
  - Qdrant: Filter(key=tenant_id)
  - 内存版: 按 tenant 分桶
"""
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class RetrievedChunk:
    chunk_id: str
    doc_id: str
    doc_title: str
    text: str
    source: str = ""
    score: float = 0.0
    tenant_id: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "doc_title": self.doc_title,
            "text": self.text,
            "source": self.source,
            "score": round(self.score, 4),
        }


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class InMemoryVectorStore:
    """纯 Python 余弦检索，接口与 Qdrant 版一致，用于离线测试。"""

    def __init__(self, dim: int = 256):
        self.dim = dim
        self._points: dict[str, dict] = {}  # point_id -> {vector, payload}

    def upsert(self, tenant_id: str, chunks: list[dict], vectors: list[list[float]]) -> None:
        for chunk, vec in zip(chunks, vectors):
            point_id = chunk["chunk_id"]
            self._points[point_id] = {"vector": vec, "payload": {**chunk, "tenant_id": tenant_id}}

    def search(self, tenant_id: str, query_vector: list[float], top_k: int = 5) -> list[RetrievedChunk]:
        scored = []
        for point in self._points.values():
            payload = point["payload"]
            if payload.get("tenant_id") != tenant_id:
                continue  # 租户隔离
            scored.append((cosine(query_vector, point["vector"]), payload))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            RetrievedChunk(
                chunk_id=p["chunk_id"],
                doc_id=p["doc_id"],
                doc_title=p["doc_title"],
                text=p["text"],
                source=p.get("source", ""),
                score=score,
                tenant_id=p["tenant_id"],
            )
            for score, p in scored[:top_k]
        ]

    def delete_doc(self, tenant_id: str, doc_id: str) -> int:
        ids = [
            pid
            for pid, point in self._points.items()
            if point["payload"].get("tenant_id") == tenant_id and point["payload"].get("doc_id") == doc_id
        ]
        for pid in ids:
            del self._points[pid]
        return len(ids)

    def count(self, tenant_id: str | None = None) -> int:
        if tenant_id is None:
            return len(self._points)
        return sum(1 for p in self._points.values() if p["payload"].get("tenant_id") == tenant_id)


class QdrantVectorStore:
    """Qdrant 封装：memory（进程内）/ local（磁盘）/ remote（独立服务）。"""

    def __init__(self, mode: str = "memory", url: str = "http://localhost:6333", collection: str = "kb_chunks", dim: int = 256):
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        if mode == "remote":
            self.client = QdrantClient(url=url, timeout=10)
        elif mode == "local":
            self.client = QdrantClient(path="./data/qdrant_local")
        else:
            self.client = QdrantClient(":memory:")
        self.collection = collection
        if not self.client.collection_exists(collection):
            self.client.create_collection(collection, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))

    def upsert(self, tenant_id: str, chunks: list[dict], vectors: list[list[float]]) -> None:
        from qdrant_client.models import PointStruct

        points = [
            PointStruct(id=chunk["chunk_id"], vector=vec, payload={**chunk, "tenant_id": tenant_id})
            for chunk, vec in zip(chunks, vectors)
        ]
        self.client.upsert(self.collection, points=points)

    def search(self, tenant_id: str, query_vector: list[float], top_k: int = 5) -> list[RetrievedChunk]:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        result = self.client.query_points(
            self.collection,
            query=query_vector,
            limit=top_k,
            query_filter=Filter(must=[FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id))]),
        )
        return [
            RetrievedChunk(
                chunk_id=str(p.payload.get("chunk_id")),
                doc_id=str(p.payload.get("doc_id", "")),
                doc_title=str(p.payload.get("doc_title", "")),
                text=str(p.payload.get("text", "")),
                source=str(p.payload.get("source", "")),
                score=p.score or 0.0,
                tenant_id=tenant_id,
            )
            for p in result.points
        ]

    def delete_doc(self, tenant_id: str, doc_id: str) -> int:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        flt = Filter(
            must=[
                FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
                FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
            ]
        )
        result = self.client.delete(self.collection, points_selector=flt)
        return 1 if getattr(result, "status", "completed") else 0

    def count(self, tenant_id: str | None = None) -> int:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        flt = None
        if tenant_id is not None:
            flt = Filter(must=[FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id))])
        result = self.client.count(self.collection, count_filter=flt, exact=True)
        return result.count


def get_vector_store(settings):
    if settings.qdrant_mode == "memory":
        return InMemoryVectorStore(dim=settings.embedding_dim)
    try:
        return QdrantVectorStore(
            mode=settings.qdrant_mode, url=settings.qdrant_url, collection=settings.qdrant_collection, dim=settings.embedding_dim
        )
    except Exception as exc:  # Qdrant 不可达时自动退化，保证服务可用
        logger.warning("Qdrant 连接失败(%s)，退化为进程内向量库", exc)
        return InMemoryVectorStore(dim=settings.embedding_dim)
