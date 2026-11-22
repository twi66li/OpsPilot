"""Qdrant 真实 SDK（:memory: 模式）回归：合法 point id、幂等覆盖、按文档删除互不影响。"""

from app.rag.vector_store import QdrantVectorStore

DIM = 8


def _vec(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


def _chunks(doc_id: str) -> list[dict]:
    return [
        {
            "chunk_id": f"{doc_id}::{i}",
            "doc_id": doc_id,
            "doc_title": f"文档{doc_id}",
            "text": f"{doc_id} 第 {i} 块",
            "source": "test",
            "chunk_index": i,
        }
        for i in range(2)
    ]


def test_qdrant_memory_upsert_search_delete():
    """真实 QdrantClient(:memory:) 走当前 QdrantVectorStore：入库→检索→幂等→删除。"""
    store = QdrantVectorStore(mode="memory", dim=DIM)
    store.upsert("t1", _chunks("docA"), [_vec(0), _vec(1)])
    store.upsert("t1", _chunks("docB"), [_vec(0), _vec(1)])
    assert store.count("t1") == 4

    hits = store.search("t1", _vec(0), top_k=10)
    assert {h.doc_id for h in hits} == {"docA", "docB"}

    # 重复入库同一文档：确定性 point id 幂等覆盖，不产生重复点
    store.upsert("t1", _chunks("docA"), [_vec(0), _vec(1)])
    assert store.count("t1") == 4

    # 删除 docA：docB 检索不受影响
    assert store.delete_doc("t1", "docA") == 1
    assert store.count("t1") == 2
    hits = store.search("t1", _vec(0), top_k=10)
    assert {h.doc_id for h in hits} == {"docB"}
