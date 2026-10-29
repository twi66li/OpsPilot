"""知识库接口：文档入库 / 查询 / 检索（支持 dense/bm25/hybrid 三种模式对比）。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.schemas import DocumentIn, SearchIn
from app.auth.deps import AuthUser, get_current_user, require_roles
from app.container import get_container
from app.db.base import get_db
from app.db.models import Document
from app.services import kb_service

router = APIRouter(prefix="/api/kb", tags=["knowledge-base"])


@router.post("/documents")
def create_document(body: DocumentIn, user: AuthUser = Depends(require_roles("admin", "engineer")), db: Session = Depends(get_db)):
    container = get_container()
    doc = Document(tenant_id=user.tenant_id, title=body.title, content=body.content, source=body.source, created_by=user.user_id)
    chunk_count = kb_service.ingest_document(db, container, doc)
    return {"id": doc.id, "title": doc.title, "chunk_count": chunk_count, "tenant_id": user.tenant_id}


@router.get("/documents")
def list_documents(user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    docs = db.query(Document).filter(Document.tenant_id == user.tenant_id).order_by(Document.created_at.desc()).limit(200).all()
    return [
        {
            "id": d.id,
            "title": d.title,
            "source": d.source,
            "chunk_count": d.chunk_count,
            "created_at": d.created_at.isoformat(),
        }
        for d in docs
    ]


@router.get("/documents/{doc_id}")
def get_document(doc_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == doc_id, Document.tenant_id == user.tenant_id).first()
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    return {"id": doc.id, "title": doc.title, "source": doc.source, "content": doc.content, "chunk_count": doc.chunk_count}


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: str, user: AuthUser = Depends(require_roles("admin", "engineer")), db: Session = Depends(get_db)):
    container = get_container()
    doc = db.query(Document).filter(Document.id == doc_id, Document.tenant_id == user.tenant_id).first()
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    kb_service.delete_document(db, container, doc)
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
