"""知识库服务：入库（分块→向量化→Qdrant/BM25 双写）与删除。"""
import logging

from sqlalchemy.orm import Session

from app.db.models import Document
from app.rag.chunking import split_into_chunks

logger = logging.getLogger(__name__)


def chunk_doc(doc: Document, chunk_size: int = 500) -> list[dict]:
    pieces = split_into_chunks(doc.content, chunk_size=chunk_size)
    return [
        {
            "chunk_id": f"{doc.id}::{i}",
            "doc_id": doc.id,
            "doc_title": doc.title,
            "source": doc.source,
            "text": piece,
            "chunk_index": i,
        }
        for i, piece in enumerate(pieces)
    ]


def ingest_document(db: Session, container, doc: Document, chunk_size: int = 500) -> int:
    """container: 依赖容器（含 embedder/vector_store/bm25）。返回分块数。"""
    db.add(doc)
    db.flush()  # 生成 doc.id（分块 ID 依赖主键）
    chunks = chunk_doc(doc, chunk_size)
    if not chunks:
        doc.chunk_count = 0
        db.add(doc)
        db.commit()
        return 0
    vectors = container.embedder.embed([c["text"] for c in chunks])
    container.vector_store.upsert(doc.tenant_id, chunks, vectors)
    container.bm25.add(doc.tenant_id, chunks)
    doc.chunk_count = len(chunks)
    db.add(doc)
    db.commit()
    logger.info("文档 %s 入库完成，%d 个分块（租户 %s）", doc.id, len(chunks), doc.tenant_id)
    return len(chunks)


def delete_document(db: Session, container, doc: Document) -> None:
    container.vector_store.delete_doc(doc.tenant_id, doc.id)
    container.bm25.remove_doc(doc.tenant_id, doc.id)
    db.delete(doc)
    db.commit()


def rebuild_bm25_from_db(db: Session, container) -> int:
    """服务启动时用 MySQL 全量文档重建 BM25 索引（按租户）。"""
    from collections import defaultdict

    by_tenant: dict[str, list[dict]] = defaultdict(list)
    for doc in db.query(Document).all():
        by_tenant[doc.tenant_id].extend(chunk_doc(doc))
    for tenant_id, chunks in by_tenant.items():
        container.bm25.replace_tenant(tenant_id, chunks)
    return sum(len(v) for v in by_tenant.values())


def warm_indexes(db: Session, container) -> int:
    """启动/评测前重建索引：BM25 全量重建；向量库为空时从 MySQL 回填向量。

    Qdrant 持久化部署时已有数据则跳过回填，避免重复 Embedding 开销；
    进程内向量库（开发/评测模式）每次都需回填，因为它不跨进程存活。
    """
    docs = db.query(Document).all()
    from collections import defaultdict

    by_tenant: dict[str, list[dict]] = defaultdict(list)
    vector_empty = getattr(container.vector_store, "count", lambda t=None: 0)() == 0
    for doc in docs:
        chunks = chunk_doc(doc)
        by_tenant[doc.tenant_id].extend(chunks)
        if vector_empty and chunks:
            vectors = container.embedder.embed([c["text"] for c in chunks])
            container.vector_store.upsert(doc.tenant_id, chunks, vectors)
    for tenant_id, chunks in by_tenant.items():
        container.bm25.replace_tenant(tenant_id, chunks)
    return sum(len(v) for v in by_tenant.values())
