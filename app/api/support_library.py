"""个人排障资料库：官方资料目录、排障场景与私人手册管理。"""

from dataclasses import replace

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.auth.deps import AuthUser, get_current_user
from app.db.base import get_db
from app.db.models import Document
from app.services.knowledge_access import personal_namespace
from app.services.official_library import BY_ID, SCENARIOS, catalog

router = APIRouter()


@router.get("/library")
def library(user: AuthUser = Depends(get_current_user)):
    return catalog()


@router.get("/scenarios")
def scenarios(user: AuthUser = Depends(get_current_user)):
    return [{**guide, "source": BY_ID[guide["doc_id"]]["url"]} for guide in SCENARIOS]


@router.get("/library/{doc_id}")
def library_document(doc_id: str, user: AuthUser = Depends(get_current_user)):
    item = BY_ID.get(doc_id)
    if not item:
        raise HTTPException(404, "资料不存在")
    return {
        "doc_id": doc_id,
        "doc_title": item["title"],
        "text": item["content"],
        "parent_text": item["content"],
        "source": item["url"],
        "source_kind": "official",
        "applicable_version": item["applicable_version"],
        "checked_at": item["checked_at"],
    }


@router.get("/private-documents")
def private_documents(user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    docs = (
        db.query(Document)
        .filter_by(tenant_id=personal_namespace(user.user_id), created_by=user.user_id, deleted_at=None)
        .all()
    )
    return [
        {
            "id": d.id,
            "title": d.title,
            "source": d.source,
            "chunk_count": d.chunk_count,
            "active_version_id": d.active_version_id,
        }
        for d in docs
    ]


@router.post("/documents/upload", status_code=202)
def upload_personal(
    file: UploadFile = File(...),
    title: str = Form(""),
    doc_id: str = Form(""),
    parsed_file: UploadFile | None = File(None),
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.api.router_kb import store_upload

    namespace = personal_namespace(user.user_id)
    return store_upload(file, title, doc_id, parsed_file, replace(user, tenant_id=namespace), db)


@router.delete("/documents/{doc_id}")
def delete_personal_document(doc_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.container import get_container
    from app.services import kb_service

    doc = (
        db.query(Document)
        .filter_by(id=doc_id, tenant_id=personal_namespace(user.user_id), created_by=user.user_id, deleted_at=None)
        .first()
    )
    if not doc:
        raise HTTPException(404, "私人手册不存在")
    try:
        kb_service.delete_document(db, get_container(), doc)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"deleted": doc_id, "history_retained": True}
