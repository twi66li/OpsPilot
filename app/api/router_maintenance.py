from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import AuthUser, get_current_user
from app.db.base import get_db
from app.db.models import ChatSession, SupportCase, WorkspaceEvent
from app.services.maintenance_service import case_report, collection_guide, record_event, timeline
from app.services.support_service import owned_case, owned_site, redact

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])


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
    return {**redact(text), "filename": file.filename, "source": "用户主动上传；预览尚未保存"}


@router.get("/cases/{case_id}/timeline")
def case_timeline(case_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    case = owned_case(db, user, case_id)
    events = timeline(db, user, "case", case_id)
    represented = {e["detail"].get("evidence_id") for e in events}
    for e in case.data.get("evidence", []):
        if e["id"] not in represented:
            events.append(
                {
                    "id": "legacy-" + e["id"],
                    "kind": "evidence_submitted",
                    "detail": {"evidence_id": e["id"], "label": e["label"]},
                    "at": e.get("submitted_at", case.created_at.isoformat()),
                }
            )
    return {"events": sorted(events, key=lambda e: e["at"])}


@router.get("/sites/{site_id}/timeline")
def site_timeline(site_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    owned_site(db, user, site_id)
    cases = (
        db.query(SupportCase.session_id)
        .join(ChatSession)
        .filter(
            SupportCase.site_id == site_id,
            SupportCase.tenant_id == user.tenant_id,
            SupportCase.user_id == user.user_id,
            ChatSession.deleted_at.is_(None),
        )
    )
    events = (
        db.query(WorkspaceEvent)
        .filter(
            WorkspaceEvent.tenant_id == user.tenant_id,
            WorkspaceEvent.user_id == user.user_id,
            ((WorkspaceEvent.resource_type == "site") & (WorkspaceEvent.resource_id == site_id))
            | ((WorkspaceEvent.resource_type == "case") & (WorkspaceEvent.resource_id.in_(cases))),
        )
        .order_by(WorkspaceEvent.id.desc())
        .limit(200)
        .all()
    )
    return {
        "events": [
            {
                "id": e.id,
                "kind": e.kind,
                "detail": e.detail,
                "at": e.created_at.isoformat(),
                "case_id": e.resource_id if e.resource_type == "case" else None,
            }
            for e in events
        ]
    }


class MetadataIn(BaseModel):
    severity: Literal["P0", "P1", "P2", "P3"] = "P2"
    labels: list[str] = Field(default_factory=list, max_length=8)


@router.put("/cases/{case_id}/metadata")
def metadata(case_id: str, body: MetadataIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    case = owned_case(db, user, case_id)
    labels = [redact(label[:30])["text"] for label in body.labels]
    case.severity, case.labels = body.severity, labels
    record_event(
        db,
        user.tenant_id,
        user.user_id,
        "case",
        case_id,
        "metadata_changed",
        {"severity": body.severity, "labels": labels, "actor": user.username},
    )
    db.commit()
    return {"severity": case.severity, "labels": case.labels}


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@router.post("/cases/{case_id}/comments")
def comment(case_id: str, body: CommentIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    owned_case(db, user, case_id)
    record_event(
        db,
        user.tenant_id,
        user.user_id,
        "case",
        case_id,
        "comment",
        {"text": redact(body.text)["text"], "actor": user.username},
    )
    db.commit()
    return {"saved": True}


@router.get("/cases/{case_id}/report")
def export_report(case_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    case = owned_case(db, user, case_id)
    title = db.get(ChatSession, case_id).title
    return Response(
        case_report(case, title, case_timeline(case_id, user, db)["events"]),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="opspilot-{case_id}.md"'},
    )


@router.post("/sites/{site_id}/http-check")
def http_check(site_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.container import get_container
    from app.services.cache import check_rate_limit
    from app.services.http_probe import probe_url

    site = owned_site(db, user, site_id)
    if not site.data.get("url"):
        raise HTTPException(400, "请先在网站资料中填写公开网址")
    if not check_rate_limit(get_container().cache, "http-probe:" + user.user_id, 5):
        raise HTTPException(429, "检查过于频繁，请一分钟后重试")
    try:
        result = probe_url(site.data["url"])
    except (ValueError, OSError) as exc:
        raise HTTPException(400, "无法探测该地址：请确认公开HTTP(S)网址、端口与DNS；禁止访问内网") from exc
    site.data = {**site.data, "health_check": result}
    record_event(db, user.tenant_id, user.user_id, "site", site_id, "http_probe", result)
    db.commit()
    return result


class RetrievalIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    doc_ids: list[str] = Field(default_factory=list, max_length=20)
    include_official: bool = True


@router.post("/retrieval-test")
def retrieval_test(body: RetrievalIn, user: AuthUser = Depends(get_current_user)):
    from app.container import get_container
    from app.services.support_service import retrieve_for_case, validate_private_docs

    container = get_container()
    with container.db_session() as db:
        validate_private_docs(db, user, body.doc_ids)
    hits = retrieve_for_case(
        container,
        user.tenant_id,
        user.user_id,
        {"profile": {"doc_ids": body.doc_ids}, "exclude_official": not body.include_official},
        body.query,
        5,
    )
    return {
        "hits": hits,
        "config": {"mode": "dense + BM25 + RRF", "rerank_enabled": container.settings.rerank_enabled},
        "note": "只检索当前有效版本和本人选中的手册，不调用聊天模型；Embedding和Rerank仍使用配置的额度",
    }


@router.get("/todos")
def todos(user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.db.models import AgentRun, ApprovalRequest, Document, DocumentVersion
    from app.services.knowledge_access import personal_namespace

    cases = (
        db.query(SupportCase, ChatSession)
        .join(ChatSession)
        .filter(
            SupportCase.tenant_id == user.tenant_id,
            SupportCase.user_id == user.user_id,
            ChatSession.deleted_at.is_(None),
            SupportCase.status.in_(["needs_information", "awaiting_results"]),
        )
        .order_by(SupportCase.updated_at.desc())
        .limit(20)
        .all()
    )
    items = [
        {
            "id": "case-" + c.session_id,
            "title": s.title,
            "state": "待补充证据" if c.status == "needs_information" else "待提交检查结果",
            "url": "/issues/" + c.session_id,
        }
        for c, s in cases
        if not c.data.get("archived") and not c.data.get("saved")
    ]
    versions = (
        db.query(DocumentVersion)
        .join(Document)
        .filter(
            DocumentVersion.tenant_id == personal_namespace(user.user_id),
            DocumentVersion.status == "failed",
            Document.deleted_at.is_(None),
        )
        .order_by(DocumentVersion.created_at.desc())
        .limit(10)
        .all()
    )
    items.extend(
        {
            "id": "doc-" + v.id,
            "title": v.filename,
            "state": "资料入库失败，可重试",
            "url": "/knowledge/" + v.doc_id + "?tab=private",
        }
        for v in versions
    )
    runs = (
        db.query(AgentRun)
        .join(ChatSession, AgentRun.session_id == ChatSession.id)
        .filter(
            ChatSession.user_id == user.user_id,
            AgentRun.tenant_id == user.tenant_id,
            ChatSession.deleted_at.is_(None),
            AgentRun.status.in_(["running", "cancel_requested", "interrupted"]),
        )
        .order_by(AgentRun.created_at.desc())
        .limit(10)
        .all()
    )
    items.extend(
        {
            "id": "run-" + r.id,
            "title": r.data.get("question", "调查")[:80],
            "state": {"running": "正在分析", "cancel_requested": "正在停止", "interrupted": "运行曾被重启中断"}[
                r.status
            ],
            "url": ("/support/" if db.get(SupportCase, r.session_id) else "/investigation/") + r.session_id,
        }
        for r in runs
    )
    finished = (
        db.query(AgentRun)
        .join(ChatSession, AgentRun.session_id == ChatSession.id)
        .filter(
            ChatSession.user_id == user.user_id,
            AgentRun.tenant_id == user.tenant_id,
            ChatSession.deleted_at.is_(None),
            AgentRun.status.in_(["completed", "answered"]),
        )
        .order_by(AgentRun.updated_at.desc())
        .limit(5)
        .all()
    )
    items.extend(
        {
            "id": "done-" + r.id,
            "title": r.data.get("question", "调查")[:80],
            "state": "最近已完成",
            "url": ("/support/" if db.get(SupportCase, r.session_id) else "/investigation/") + r.session_id,
        }
        for r in finished
    )
    if user.role in ("admin", "engineer"):
        approvals = db.query(ApprovalRequest).filter_by(tenant_id=user.tenant_id, status="pending").limit(20).all()
        items.extend(
            {
                "id": "approval-" + r.id,
                "title": r.ticket_draft.get("title", "工单申请"),
                "state": "待审批",
                "url": "/operations/" + r.id,
            }
            for r in approvals
        )
    return {"items": items, "total": len(items), "scope": "最多展示最近60项可处理记录；不是未读消息数"}


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


@router.get("/operations/{record_id}")
def operation_detail(record_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.db.models import AgentRun, ApprovalRequest, RemediationRequest, Ticket
    from app.services.run_service import serialize_run

    if user.role not in ("admin", "engineer"):
        raise HTTPException(403, "需要工程师或管理员权限")
    row = None
    kind = ""
    for model, name in ((ApprovalRequest, "ticket_approval"), (RemediationRequest, "remediation"), (Ticket, "ticket")):
        row = db.query(model).filter_by(id=record_id, tenant_id=user.tenant_id).first()
        if row:
            kind = name
            break
    if row is None:
        raise HTTPException(404, "审批或工单不存在")
    data = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    for key, value in data.items():
        if hasattr(value, "isoformat"):
            data[key] = value.isoformat()
    run = (
        db.query(AgentRun).filter_by(id=row.run_id, tenant_id=user.tenant_id).first()
        if kind == "remediation"
        else db.query(AgentRun)
        .filter_by(session_id=row.session_id, tenant_id=user.tenant_id)
        .order_by(AgentRun.created_at.desc())
        .first()
    )
    # 这是组织审批入口，按本租户工程师权限读取对应演练调查，不开放个人排障记录。
    return {
        "kind": kind,
        "record": data,
        "run": serialize_run(db, run) if run else None,
        "impact": "仅关闭order-service测试故障注入；不操作用户网站或生产环境"
        if kind == "remediation"
        else "仅创建或查看跟进工单；不执行修复命令",
        "verification_scope": "验证仅对应所记录的请求样本，不能证明生产根因消除或长期恢复",
    }
