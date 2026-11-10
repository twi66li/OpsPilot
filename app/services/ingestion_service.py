"""单进程、单并发入库任务；状态持久化，重启中断的任务可显式重试。"""
import logging
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import version as package_version

from app.db.base import SessionLocal
from app.db.models import Document, DocumentChunk, DocumentParent, DocumentVersion
from app.services.document_parser import build_version_chunks, parse_file

logger = logging.getLogger(__name__)
_worker: ThreadPoolExecutor | None = None


def start_worker():
    global _worker
    with SessionLocal() as db:
        interrupted = db.query(DocumentVersion).filter(DocumentVersion.status.notin_(["ready", "failed"]))
        interrupted.update({"status": "failed", "error": "服务重启中断了入库，请重试"}, synchronize_session=False)
        db.commit()
    _worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="knowledge-ingest")


def stop_worker():
    global _worker
    if _worker:
        _worker.shutdown(wait=True)
        _worker = None


def enqueue(version_id: str, container):
    if _worker is None:
        raise RuntimeError("入库工作线程尚未启动")
    _worker.submit(process_version, version_id, container)


def process_version(version_id: str, container):
    with SessionLocal() as db:
        version = db.get(DocumentVersion, version_id)
        if version is None:
            return
        try:
            doc = db.get(Document, version.doc_id)
            version.status, version.progress, version.error = "parsing", 10, ""
            db.commit()
            pages, warnings, parser = parse_file(version.original_path, version.filename, version.parsed_path)
            version.pages, version.warnings, version.parser = pages, warnings, parser
            version.status, version.progress = "chunking", 30
            db.commit()
            parents, chunks = build_version_chunks(doc, version, pages)
            if not chunks:
                raise ValueError("未生成可检索片段，请检查正文")
            db.query(DocumentChunk).filter_by(version_id=version.id).delete()
            db.query(DocumentParent).filter_by(version_id=version.id).delete()
            for parent in parents:
                db.add(DocumentParent(tenant_id=doc.tenant_id, doc_id=doc.id, version_id=version.id, **parent))
            for chunk in chunks:
                db.add(DocumentChunk(id=chunk["chunk_id"], tenant_id=doc.tenant_id, doc_id=doc.id,
                                     version_id=version.id, parent_id=chunk["parent_id"], payload=chunk))
            version.index_config = {**version.index_config, "embedding_model": container.settings.embedding_model,
                                    "embedding_dim": container.settings.embedding_dim,
                                    "chunking_version": "structure-v1", "chunk_count": len(chunks),
                                    "parser_version": package_version("pypdf") if parser == "pypdf" else "v1"}
            version.status, version.progress = "indexing", 45
            db.commit()
            for offset in range(0, len(chunks), 16):
                batch = chunks[offset:offset + 16]
                vectors = container.embedder.embed([c["text"] for c in batch])
                if len(vectors) != len(batch):
                    raise ValueError("Embedding返回数量与片段数量不一致")
                container.vector_store.upsert(doc.tenant_id, batch, vectors)
                version.progress = 45 + int(50 * min(offset + 16, len(chunks)) / len(chunks))
                db.commit()
            container.bm25.add(doc.tenant_id, chunks)
            # 召回在两路搜索前都按此指针过滤；失败/构建中的版本永不替换旧版。
            doc.active_version_id = version.id
            doc.content = "\n\n".join(p["text"] for p in pages)
            doc.chunk_count = len(chunks)
            version.status, version.progress = "ready", 100
            db.commit()
        except Exception as exc:
            db.rollback()
            version = db.get(DocumentVersion, version_id)
            if version:
                version.error = f"{version.status}阶段失败：{type(exc).__name__}，请检查文档或服务后重试"
                if isinstance(exc, ValueError):
                    version.error += f"；{str(exc)[:300]}"
                version.status = "failed"
                db.commit()
            logger.exception("知识版本 %s 入库失败", version_id)
