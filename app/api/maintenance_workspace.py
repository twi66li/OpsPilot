"""维护工作台：能力概览、采集指引、日志文件预览、活动流、检索测试、待办与失败重试。"""

from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.router_kb import require_retrieval_allowance
from app.auth.deps import AuthUser, get_current_user
from app.db.base import get_db
from app.services.assistant_quota import model_allowance
from app.services.budget import BudgetExceededError
from app.services.evidence_checks import summarize_evidence
from app.services.maintenance_report import collection_guide
from app.services.maintenance_timeline import list_recent_activity
from app.services.maintenance_workspace import get_workspace_capabilities
from app.services.support_evidence import redact, validate_private_docs
from app.services.support_retrieval import retrieve_for_case
from app.services.todo_service import todo_items

router = APIRouter()


@router.get("/capabilities")
def capabilities(user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return get_workspace_capabilities(db, user)


class GuideIn(BaseModel):
    scenario: Literal["nginx", "pool", "container"]
    deployment: Literal["docker", "systemd", "unknown"] = "unknown"
    target: str = Field(default="", max_length=100)


@router.post("/collection-guide")
def guide(body: GuideIn, user: AuthUser = Depends(get_current_user)):
    try:
        return {"steps": collection_guide(body.scenario, body.deployment, body.target), "execution": "用户自行执行"}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/evidence/file-preview")
async def preview_file(file: UploadFile = File(...), user: AuthUser = Depends(get_current_user)):
    if not (file.filename or "").lower().endswith((".txt", ".log", ".json")):
        raise HTTPException(400, "请选择TXT、LOG或JSON日志文件")
    raw = await file.read(65537)
    if len(raw) > 65536:
        raise HTTPException(413, "日志文件过大，请导出故障附近的片段，最多64KB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "请将日志另存为UTF-8编码") from exc
    if not text.strip() or len(text) > 12000:
        raise HTTPException(400, "日志片段需要1～12000字符，请缩小到异常附近")
    result = redact(text)
    return {
        **result,
        "summary": summarize_evidence({"id": "preview", "kind": "initial", "text": result["text"]}),
        "filename": file.filename,
        "source": "用户主动上传；预览尚未保存",
    }


@router.get("/activity")
def recent_activity(user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return list_recent_activity(db, user)


class RetrievalIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    doc_ids: list[str] = Field(default_factory=list, max_length=20)
    include_official: bool = True


@router.post("/retrieval-test")
def retrieval_test(body: RetrievalIn, user: AuthUser = Depends(require_retrieval_allowance)):
    from app.container import get_container

    container = get_container()
    with container.db_session() as db:
        validate_private_docs(db, user, body.doc_ids)
    try:
        with model_allowance(user.user_id, user.tenant_id, user.role):
            hits = retrieve_for_case(
                container,
                user.tenant_id,
                user.user_id,
                {"profile": {"doc_ids": body.doc_ids}, "exclude_official": not body.include_official},
                body.query,
                5,
            )
    except BudgetExceededError as exc:
        raise HTTPException(429, str(exc)) from exc
    return {
        "hits": hits,
        "config": {"mode": "dense + BM25 + RRF", "rerank_enabled": container.settings.rerank_enabled},
        "note": "只检索当前有效版本和本人选中的手册，不调用聊天模型；Embedding和Rerank仍使用配置的额度",
    }


@router.get("/todos")
def todos(user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return todo_items(db, user)


class RetryIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=20)


@router.post("/documents/retry-failed")
def retry_failed(body: RetryIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.api.router_kb import retry_version
    from app.db.models import Document, DocumentVersion
    from app.services.knowledge_access import personal_namespace

    docs = (
        db.query(Document)
        .filter(
            Document.id.in_(body.ids),
            Document.tenant_id == personal_namespace(user.user_id),
            Document.created_by == user.user_id,
            Document.deleted_at.is_(None),
        )
        .all()
    )
    if {d.id for d in docs} != set(body.ids):
        raise HTTPException(404, "部分手册不存在或不属于当前账号")
    results = []
    for doc in docs:
        latest = db.query(DocumentVersion).filter_by(doc_id=doc.id).order_by(DocumentVersion.number.desc()).first()
        if latest is None or latest.status != "failed":
            results.append({"id": doc.id, "status": "skipped"})
            continue
        try:
            retry_version(latest.id, user, db)
            results.append({"id": doc.id, "status": "queued"})
        except HTTPException:
            results.append({"id": doc.id, "status": "blocked"})
    return {"items": results}
