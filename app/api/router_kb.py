"""知识库接口：文档入库 / 查询 / 检索（支持 dense/bm25/hybrid 三种模式对比）。"""
import hashlib
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.api.schemas import DocumentIn, SearchIn
from app.auth.deps import AuthUser, get_current_user, require_roles
from app.container import get_container
from app.db.async_session import get_async_db
from app.db.base import get_db
from app.db.models import Document, DocumentChunk, DocumentParent, DocumentVersion
from app.services import kb_service
from app.services.ingestion_service import enqueue
from app.services.knowledge_access import personal_namespace, readable_namespaces

router = APIRouter(prefix="/api/kb", tags=["knowledge-base"])


def _version_dict(version, doc):
    return {"id": version.id, "version_id": version.id, "doc_id": version.doc_id, "number": version.number,
            "filename": version.filename, "content_hash": version.content_hash, "parser": version.parser,
            "status": version.status, "progress": version.progress, "error": version.error,
            "warnings": version.warnings, "index_config": version.index_config,
            "active": doc.active_version_id == version.id, "created_at": version.created_at.isoformat()}


def _get_version(db, version_id, user):
    version = db.query(DocumentVersion).filter(DocumentVersion.id == version_id,
                                              DocumentVersion.tenant_id.in_(readable_namespaces(user))).first()
    if version is None:
        raise HTTPException(404, "知识版本不存在")
    return version, db.get(Document, version.doc_id)


@router.post("/documents/upload", status_code=202)
def upload_document(file: UploadFile = File(...), title: str = Form(""), doc_id: str = Form(""),
                    parsed_file: UploadFile | None = File(None),
                    user: AuthUser = Depends(require_roles("admin", "engineer")), db: Session = Depends(get_db)):
    filename = Path((file.filename or "").replace("\\", "/")).name
    if len(filename) > 200 or Path(filename).suffix.lower() not in (".pdf", ".md", ".txt"):
        raise HTTPException(400, "请上传PDF、Markdown或TXT文件，文件名不超过200字符")
    data = file.file.read(10 * 1024 * 1024 + 1)
    parsed_data = parsed_file.file.read(10 * 1024 * 1024 + 1) if parsed_file else b""
    if not data or len(data) > 10 * 1024 * 1024 or len(parsed_data) > 10 * 1024 * 1024:
        raise HTTPException(400, "原文件和解析文件各不超过10MB，原文件不能为空")
    if parsed_file and (Path(filename).suffix.lower() != ".pdf" or not parsed_data):
        raise HTTPException(400, "MinerU解析结果必须与原PDF一同上传")
    title = title.strip() or Path(filename).stem
    if len(title) > 200:
        raise HTTPException(400, "标题不超过200字符")
    digest = hashlib.sha256(data).hexdigest()
    input_hash = hashlib.sha256(data + b"\0" + parsed_data).hexdigest()
    existing = db.query(DocumentVersion).filter_by(tenant_id=user.tenant_id, content_hash=digest).all()
    for version in existing:
        if (not doc_id or version.doc_id == doc_id) and version.index_config.get("input_hash") == input_hash:
            return {**_version_dict(version, db.get(Document, version.doc_id)), "duplicate": True}
    if doc_id:
        doc = db.query(Document).filter_by(id=doc_id, tenant_id=user.tenant_id).with_for_update().first()
        if not doc:
            raise HTTPException(404, "文档不存在")
        running = db.query(DocumentVersion).filter(DocumentVersion.doc_id == doc_id,
                                                  DocumentVersion.status.notin_(["ready", "failed"])).first()
        if running:
            raise HTTPException(409, "该文档已有入库任务，请等待完成后再上传版本")
    else:
        doc = Document(tenant_id=user.tenant_id, title=title, content="", source=filename, created_by=user.user_id)
        db.add(doc)
        db.flush()
    number = (db.query(func.max(DocumentVersion.number)).filter_by(doc_id=doc.id).scalar() or 0) + 1
    version_id = uuid.uuid4().hex
    folder = Path(get_container().settings.kb_storage_path).resolve() / user.tenant_id / version_id
    folder.mkdir(parents=True, exist_ok=True)
    original = folder / ("original" + Path(filename).suffix.lower())
    original.write_bytes(data)
    parsed_path = ""
    if parsed_file:
        parsed = folder / "content_list.json"
        parsed.write_bytes(parsed_data)
        parsed_path = str(parsed)
    version = DocumentVersion(id=version_id, tenant_id=user.tenant_id, doc_id=doc.id, number=number,
                              filename=filename, content_hash=digest, original_path=str(original),
                              parsed_path=parsed_path, index_config={"input_hash": input_hash})
    db.add(version)
    db.commit()
    response = _version_dict(version, doc)
    enqueue(version.id, get_container())
    return response


@router.get("/versions/{version_id}")
async def get_version(version_id: str, user: AuthUser = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    version, doc = await db.run_sync(_get_version, version_id, user)
    chunks = (await db.scalars(select(DocumentChunk).filter_by(version_id=version.id, tenant_id=version.tenant_id))).all()
    return {**_version_dict(version, doc), "pages": version.pages, "chunks": [c.payload for c in chunks]}


@router.get("/versions/{version_id}/file")
async def get_original(version_id: str, user: AuthUser = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    version, _ = await db.run_sync(_get_version, version_id, user)
    if not Path(version.original_path).is_file():
        raise HTTPException(404, "原文件不存在，请检查持久卷")
    return FileResponse(version.original_path, filename=version.filename, media_type="application/octet-stream")


@router.get("/versions/{version_id}/status")
async def version_status(version_id: str, user: AuthUser = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    version, doc = await db.run_sync(_get_version, version_id, user)
    return _version_dict(version, doc)


@router.post("/versions/{version_id}/retry", status_code=202)
def retry_version(version_id: str, user: AuthUser = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    version, doc = _get_version(db, version_id, user)
    if version.tenant_id != personal_namespace(user.user_id) and user.role not in ("admin", "engineer"):
        raise HTTPException(403, "无权修改共享文档")
    updated = db.query(DocumentVersion).filter_by(id=version.id, status="failed").update(
        {"status": "uploaded", "progress": 0, "error": ""}, synchronize_session=False)
    if not updated:
        raise HTTPException(409, "只有失败任务可以重试")
    db.commit()
    db.refresh(version)
    response = _version_dict(version, doc)
    enqueue(version.id, get_container())
    return response


@router.post("/versions/{version_id}/activate")
def activate_version(version_id: str, user: AuthUser = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    version, doc = _get_version(db, version_id, user)
    if version.tenant_id != personal_namespace(user.user_id) and user.role not in ("admin", "engineer"):
        raise HTTPException(403, "无权修改共享文档")
    if version.status != "ready":
        raise HTTPException(409, "只有完成索引的版本可以生效")
    doc.active_version_id = version.id
    doc.content = "\n\n".join(p["text"] for p in version.pages)
    doc.chunk_count = version.index_config["chunk_count"]
    db.commit()
    return _version_dict(version, doc)


@router.get("/chunks/{chunk_id}")
async def get_chunk(chunk_id: str, user: AuthUser = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    chunk = await db.scalar(select(DocumentChunk).where(DocumentChunk.id == chunk_id,
                                                       DocumentChunk.tenant_id.in_(readable_namespaces(user))))
    if chunk is None:
        raise HTTPException(404, "片段不存在")
    parent = await db.scalar(select(DocumentParent).filter_by(id=chunk.parent_id, tenant_id=chunk.tenant_id))
    version, doc = await db.run_sync(_get_version, chunk.version_id, user)
    return {**chunk.payload, "parent_text": parent.text, "active": version.id == doc.active_version_id,
            "file_url": f"/api/kb/versions/{version.id}/file"}


@router.post("/documents")
def create_document(body: DocumentIn, user: AuthUser = Depends(require_roles("admin", "engineer")), db: Session = Depends(get_db)):
    container = get_container()
    doc = Document(tenant_id=user.tenant_id, title=body.title, content=body.content, source=body.source, created_by=user.user_id)
    chunk_count = kb_service.ingest_document(db, container, doc)
    return {"id": doc.id, "title": doc.title, "chunk_count": chunk_count, "tenant_id": user.tenant_id}


@router.get("/documents")
async def list_documents(user: AuthUser = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    docs = (await db.scalars(select(Document).filter_by(tenant_id=user.tenant_id).order_by(Document.created_at.desc()).limit(200))).all()
    return [
        {
            "id": d.id,
            "title": d.title,
            "source": d.source,
            "chunk_count": d.chunk_count,
            "active_version_id": d.active_version_id,
            "created_at": d.created_at.isoformat(),
        }
        for d in docs
    ]


@router.get("/documents/{doc_id}")
async def get_document(doc_id: str, user: AuthUser = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    doc = await db.scalar(select(Document).where(Document.id == doc_id, Document.tenant_id.in_(readable_namespaces(user))))
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    return {"id": doc.id, "title": doc.title, "source": doc.source, "content": doc.content, "chunk_count": doc.chunk_count}


@router.get("/documents/{doc_id}/versions")
async def list_versions(doc_id: str, user: AuthUser = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    doc = await db.scalar(select(Document).where(Document.id == doc_id, Document.tenant_id.in_(readable_namespaces(user))))
    if doc is None:
        raise HTTPException(404, "文档不存在")
    versions = (await db.scalars(select(DocumentVersion).filter_by(doc_id=doc_id).order_by(DocumentVersion.number.desc()))).all()
    return [_version_dict(v, doc) for v in versions]


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: str, user: AuthUser = Depends(require_roles("admin", "engineer")), db: Session = Depends(get_db)):
    container = get_container()
    doc = db.query(Document).filter(Document.id == doc_id, Document.tenant_id == user.tenant_id).first()
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    try:
        kb_service.delete_document(db, container, doc)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"deleted": doc_id}


@router.post("/search")
def search(body: SearchIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """检索接口：mode=dense|bm25|hybrid 可对比不同检索方案。"""
    if body.mode not in ("dense", "bm25", "hybrid"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "mode 必须是 dense/bm25/hybrid")
    container = get_container()
    result = container.retriever.retrieve(db, user.tenant_id, body.query, mode=body.mode, top_k=body.top_k)
    return {
        "mode": result.mode,
        "reranked": result.reranked,
        "latency_ms": round(result.latency_ms, 1),
        "hits": [h.to_dict() for h in result.hits],
        "debug": {
            "dense_count": len(result.dense_raw),
            "bm25_count": len(result.bm25_raw),
            "dense_titles": [c.doc_title for c in result.dense_raw[:5]],
            "bm25_titles": [c.doc_title for c in result.bm25_raw[:5]],
        },
    }
