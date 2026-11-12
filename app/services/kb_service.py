"""知识库服务：入库（分块→向量化→Qdrant/BM25 双写）与删除。"""

import logging

from sqlalchemy.orm import Session

from app.db.models import Document, DocumentChunk, DocumentVersion, WebsiteProfile, _now
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
    running = (
        db.query(DocumentVersion)
        .filter(DocumentVersion.doc_id == doc.id, DocumentVersion.status.notin_(["ready", "failed"]))
        .first()
    )
    if running:
        raise ValueError("文档正在入库，请等待任务结束后删除")
    doc.deleted_at, doc.active_version_id, doc.chunk_count = _now(), None, 0
    for site in db.query(WebsiteProfile).filter_by(user_id=doc.created_by, deleted_at=None).all():
        if doc.id in site.data.get("doc_ids", []):
            site.data = {**site.data, "doc_ids": [doc_id for doc_id in site.data["doc_ids"] if doc_id != doc.id]}
    db.commit()  # 检索先按数据库有效文档过滤；清理索引失败也不会召回已删除资料。
    container.vector_store.delete_doc(doc.tenant_id, doc.id)
    container.bm25.remove_doc(doc.tenant_id, doc.id)


def rebuild_bm25_from_db(db: Session, container) -> int:
    """用与向量库同源同界的分块重建 BM25 索引（按租户）。"""
    from collections import defaultdict

    tenants = sorted({doc.tenant_id for doc in db.query(Document).all()})
    by_tenant: dict[str, list[dict]] = defaultdict(list)
    for tenant_id in tenants:
        by_tenant[tenant_id].extend(container.vector_store.scroll_tenant(tenant_id))
    for tenant_id, chunks in by_tenant.items():
        container.bm25.replace_tenant(tenant_id, chunks)
    return sum(len(v) for v in by_tenant.values())


def warm_indexes(db: Session, container) -> int:
    """启动/评测前重建索引：BM25 全量重建；向量库为空时从 MySQL 回填向量。

    BM25 分块一律取自向量库已有 payload（与稠密检索同源同界）。此前从 MySQL 重新
    分块（默认 500 字符）会与入库时分块（如 n31real 语料的 2000 字符）漂移：
    两路 chunk 边界不一致，混合检索把同内容不同边界的块当作不同证据融合。
    向量库为空时仍按 MySQL 默认分块回填（重新 embedding），回填后两路自动同源。
    """
    from collections import defaultdict

    docs = db.query(Document).filter_by(deleted_at=None).all()
    vector_empty = getattr(container.vector_store, "count", lambda t=None: 0)() == 0
    if vector_empty:
        for doc in docs:
            versions = db.query(DocumentVersion.id).filter_by(doc_id=doc.id, status="ready").all()
            chunks = (
                [
                    c.payload
                    for c in db.query(DocumentChunk)
                    .filter(DocumentChunk.version_id.in_([v.id for v in versions]))
                    .all()
                ]
                if versions
                else chunk_doc(doc)
            )
            if chunks:
                vectors = container.embedder.embed([c["text"] for c in chunks])
                container.vector_store.upsert(doc.tenant_id, chunks, vectors)
    by_tenant: dict[str, list[dict]] = defaultdict(list)
    for tenant_id in sorted({doc.tenant_id for doc in docs}):
        by_tenant[tenant_id].extend(container.vector_store.scroll_tenant(tenant_id))
    for tenant_id, chunks in by_tenant.items():
        container.bm25.replace_tenant(tenant_id, chunks)
    return sum(len(v) for v in by_tenant.values())
