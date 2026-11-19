from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import case as sql_case
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.api.router_kb import require_retrieval_allowance
from app.auth.deps import AuthUser, get_current_user
from app.db.base import get_db
from app.db.models import (
    CaseHandoff,
    ChatSession,
    SupportCase,
    User,
    WebsiteMonitor,
    WebsiteProfile,
    WorkspaceEvent,
    _now,
    _uuid,
)
from app.services import website_monitor
from app.services.evidence_checks import analyze_samples, summarize_evidence
from app.services.maintenance_service import (
    case_report,
    collection_guide,
    event_at,
    event_dict,
    recent_site_changes,
    record_event,
    sort_events,
    timeline,
)
from app.services.support_service import owned_case, owned_site, redact

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])


@router.get("/capabilities")
def capabilities(user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.api.router_health import readiness_checks
    from app.config import get_settings
    from app.db.models import AgentRun

    settings = get_settings()
    checks = readiness_checks()
    scheduled = db.query(WebsiteMonitor.site_id).join(WebsiteProfile).filter(
        WebsiteProfile.tenant_id == user.tenant_id, WebsiteProfile.user_id == user.user_id,
        WebsiteProfile.deleted_at.is_(None), WebsiteMonitor.enabled.is_(True),
    ).first()
    configured = settings.llm_provider == "openai" and bool(settings.openai_api_key)
    knowledge_ready = checks.get("database") == "ok" and checks.get("vector_store") in ("ok", "memory")
    items = [
        {"key": "analysis", "title": "维护助手", "state": "read_only" if user.is_guest else "configured" if configured else "offline",
         "description": "访客只能浏览教学记录；使用个人账号后可提交日志并分析。" if user.is_guest else
         "已配置聊天模型；这里未发起模型请求，实际可用性以分析结果为准。" if configured else "当前是离线模型模式，可以准备网站和日志，暂不能进行真实分析。"},
        {"key": "knowledge", "title": "资料与检索", "state": "available" if knowledge_ready else "unavailable",
         "description": "检索只使用可访问的官方资料和当前网站选中的私人手册；入库状态单独检查。"},
        {"key": "website", "title": "个人网站观测", "state": "available" if scheduled else "manual",
         "description": "已开启网站定时 HTTP 巡检。分析可结合巡检证据和提交的日志。" if scheduled else
         "可手动检查公开网址或提交日志。你也可以在网站详情开启定时 HTTP 巡检。"},
        {"key": "demo", "title": "独立故障演练", "state": "configured" if settings.metrics_provider == "prometheus" else "offline",
         "description": "指标与日志配置仅用于平台自带的测试服务，与个人网站分开；配置存在不代表本次查询已成功。"},
    ]
    runs = (db.query(AgentRun).join(ChatSession, AgentRun.session_id == ChatSession.id)
            .filter(AgentRun.tenant_id == user.tenant_id, ChatSession.user_id == user.user_id, ChatSession.deleted_at.is_(None))
            .order_by(AgentRun.updated_at.desc()).limit(5).all())
    reasons = {"model_error": "模型请求未完成", "budget_exceeded": "触及当前用量限制", "tool_error": "工具查询未完成",
               "answered": "已保存回答", "cancelled": "已停止", "interrupted": "服务重启中断了执行"}
    recent = []
    for run in runs:
        personal = db.get(SupportCase, run.session_id)
        reason = run.data.get("result", {}).get("end_reason") or run.status
        recent.append({"id": run.id, "question": run.data.get("question", "未记录问题")[:100], "status": run.status,
                       "reason": reasons.get(reason, "未记录具体结束原因"), "updated_at": run.updated_at.isoformat(),
                       "url": ("/support/" if personal else "/investigation/") + run.session_id})
    return {"items": items, "checks": checks, "recent": recent}


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
    return {**result, "summary": summarize_evidence({"id": "preview", "kind": "initial", "text": result["text"]}),
            "filename": file.filename, "source": "用户主动上传；预览尚未保存"}


@router.get("/cases/{case_id}/timeline")
def case_timeline(case_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    case = owned_case(db, user, case_id)
    events = timeline(db, user, "case", case_id)
    events.extend(recent_site_changes(db, user.tenant_id, user.user_id, case.site_id))
    represented = {e["detail"].get("evidence_id") for e in events}
    for e in case.data.get("evidence", []):
        if e["id"] not in represented:
            events.append(
                {
                    "id": "legacy-" + e["id"],
                    "kind": "evidence_submitted",
                    "detail": {"evidence_id": e["id"], "label": e["label"]},
                    "at": event_at(e.get("submitted_at"), case.created_at),
                }
            )
    events.extend(observed_events(case))
    return {"events": sort_events(events), "site_id": case.site_id}


def observed_events(case):
    events = []
    for evidence in case.data.get("evidence", []):
        if not evidence.get("text"):
            continue
        span = summarize_evidence(evidence)["time_range"]
        if span:
            events.append({"id": f'observed-{case.session_id}-{evidence["id"]}', "kind": "evidence_observed",
                           "case_id": case.session_id, "at": span["from"],
                           "detail": {"label": evidence["label"], "evidence_id": evidence["id"], "until": span["until"],
                                      "note": "定时 HTTP 巡检采样时间" if evidence.get("source_kind") == "scheduled_http_probe"
                                      else "已提交日志的样本时间"}})
    return events


class ChangeIn(BaseModel):
    change_kind: Literal["release", "config", "database", "maintenance"]
    title: str = Field(min_length=1, max_length=100)
    occurred_at: AwareDatetime
    version: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=1500)
    rollback: str = Field(default="", max_length=600)


@router.post("/sites/{site_id}/changes", status_code=201)
def add_change(site_id: str, body: ChangeIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    from datetime import timezone

    owned_site(db, user, site_id)
    detail = {key: redact(value)["text"] for key, value in body.model_dump(exclude={"occurred_at"}).items()}
    detail.update(occurred_at=body.occurred_at.astimezone(timezone.utc).isoformat(), actor=user.username,
                  source="用户手工记录；时间接近不代表因果关系")
    record_event(db, user.tenant_id, user.user_id, "site", site_id, "site_change", detail)
    db.commit()
    return {"recorded": True}


@router.get("/sites/{site_id}/timeline")
def site_timeline(site_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    owned_site(db, user, site_id)
    cases = (
        db.query(SupportCase)
        .join(ChatSession)
        .filter(
            SupportCase.site_id == site_id,
            SupportCase.tenant_id == user.tenant_id,
            SupportCase.user_id == user.user_id,
            ChatSession.deleted_at.is_(None),
        )
    ).all()
    case_ids = [row.session_id for row in cases]
    events = (
        db.query(WorkspaceEvent)
        .filter(
            WorkspaceEvent.tenant_id == user.tenant_id,
            WorkspaceEvent.user_id == user.user_id,
            ((WorkspaceEvent.resource_type == "site") & (WorkspaceEvent.resource_id == site_id))
            | ((WorkspaceEvent.resource_type == "case") & (WorkspaceEvent.resource_id.in_(case_ids))),
        )
        .order_by(WorkspaceEvent.id.desc())
        .limit(200)
        .all()
    )
    combined = [event_dict(e) for e in events]
    combined.extend(event for case in cases for event in observed_events(case))
    return {"events": sort_events(combined), "site_id": site_id}


@router.get("/activity")
def recent_activity(user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    sites = {
        site.id: site.data["name"] for site in db.query(WebsiteProfile)
        .filter_by(tenant_id=user.tenant_id, user_id=user.user_id, deleted_at=None).all()
    }
    events = db.query(WorkspaceEvent).filter(
        WorkspaceEvent.tenant_id == user.tenant_id, WorkspaceEvent.user_id == user.user_id,
        WorkspaceEvent.resource_type == "site", WorkspaceEvent.resource_id.in_(sites),
        WorkspaceEvent.kind.in_(("site_change", "http_incident", "http_probe_normal")),
    ).all()
    activity = [
        {**event_dict(event), "siteId": event.resource_id, "siteName": sites[event.resource_id]}
        for event in events
    ]
    cases = db.query(SupportCase).join(ChatSession).filter(
        SupportCase.tenant_id == user.tenant_id, SupportCase.user_id == user.user_id,
        SupportCase.site_id.in_(sites), ChatSession.deleted_at.is_(None),
    ).all()
    activity.extend({**event, "at": event["detail"]["until"], "siteId": case.site_id, "siteName": sites[case.site_id]}
                    for case in cases for event in observed_events(case))
    return {"events": sort_events(activity)[-4:][::-1]}


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


class HttpMonitorIn(BaseModel):
    enabled: bool
    interval_minutes: Literal[5, 15, 30, 60] = 15


@router.put("/sites/{site_id}/http-monitor")
def configure_http_monitor(site_id: str, body: HttpMonitorIn, user: AuthUser = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    site = owned_site(db, user, site_id)
    try:
        return website_monitor.configure(db, site, body.enabled, body.interval_minutes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class HandoffIn(BaseModel):
    recipient: str = Field(min_length=1, max_length=64)
    note: str = Field(min_length=1, max_length=2000)


@router.put("/cases/{case_id}/handoff")
def assign_handoff(case_id: str, body: HandoffIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    case = owned_case(db, user, case_id)
    if case.data.get("kind", "problem") != "problem" or not body.note.strip():
        raise HTTPException(400, "请选择问题并填写交接备注")
    recipient = db.query(User).filter_by(tenant_id=user.tenant_id, username=body.recipient.strip()).first()
    if not recipient or recipient.id == user.user_id:
        raise HTTPException(404, "请填写本工作区内另一位成员的用户名")
    report = case.data.get("report") or {}
    snapshot = {
        "title": db.get(ChatSession, case_id).title,
        "site_name": case.data.get("profile", {}).get("name"),
        "problem": {key: case.data.get("problem", {}).get(key) for key in ("symptom", "endpoint", "started_at")},
        "summary": redact(report.get("summary", ""))["text"],
        "evidence": [
            {key: item.get(key) for key in ("id", "label", "kind", "collected_at", "source_kind")} |
            {"text": redact(item["text"])["text"]} for item in case.data.get("evidence", [])
        ],
        "verification": analyze_samples(case.data)["verification"],
    }
    db.get(SupportCase, case_id, with_for_update=True)
    handoff = db.get(CaseHandoff, case_id, with_for_update=True) or CaseHandoff(case_id=case_id, tenant_id=user.tenant_id)
    handoff.version, handoff.status, handoff.recipient_id = _uuid(), "pending", recipient.id
    handoff.data = {"recipient": recipient.username, "sender": user.username, "note": redact(body.note.strip())["text"],
                    "shared_at": _now().isoformat() + "Z", "snapshot": snapshot}
    db.add(handoff)
    record_event(db, user.tenant_id, user.user_id, "case", case_id, "handoff_assigned",
                 {"recipient": recipient.username, "actor": user.username})
    db.commit()
    return {key: value for key, value in handoff.data.items() if key != "snapshot"} | {"status": handoff.status}


@router.delete("/cases/{case_id}/handoff")
def revoke_handoff(case_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    owned_case(db, user, case_id)
    handoff = db.get(CaseHandoff, case_id, with_for_update=True)
    if handoff:
        db.delete(handoff)
        record_event(db, user.tenant_id, user.user_id, "case", case_id, "handoff_revoked", {"actor": user.username})
        db.commit()
    return {"revoked": True}


def _received_handoffs(db, user):
    return db.query(CaseHandoff).join(ChatSession, ChatSession.id == CaseHandoff.case_id).filter(
        CaseHandoff.tenant_id == user.tenant_id, CaseHandoff.recipient_id == user.user_id,
        ChatSession.tenant_id == user.tenant_id, ChatSession.deleted_at.is_(None),
    )


@router.get("/handoffs")
def received_handoffs(user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"items": [{"case_id": row.case_id, "version": row.version, "status": row.status, **row.data}
                      for row in _received_handoffs(db, user).order_by(CaseHandoff.data["shared_at"].as_string().desc()).limit(30)]}


class HandoffReviewIn(BaseModel):
    version: str = Field(min_length=1, max_length=64)
    status: Literal["reviewed", "changes_requested"]
    note: str = Field(min_length=1, max_length=2000)


@router.post("/handoffs/{case_id}/review")
def review_handoff(case_id: str, body: HandoffReviewIn, user: AuthUser = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    row = _received_handoffs(db, user).filter(CaseHandoff.case_id == case_id).first()
    if not row:
        raise HTTPException(404, "交接记录不存在或已撤回")
    if not body.note.strip():
        raise HTTPException(400, "请填写复核意见")
    note = redact(body.note.strip())["text"]
    changed = db.execute(update(CaseHandoff).where(
        CaseHandoff.case_id == case_id, CaseHandoff.tenant_id == user.tenant_id, CaseHandoff.recipient_id == user.user_id,
        CaseHandoff.version == body.version, CaseHandoff.status == "pending",
    ).values(status=body.status, data={**row.data, "review_note": note, "reviewed_at": _now().isoformat() + "Z"}))
    if changed.rowcount != 1:
        raise HTTPException(409, "交接状态已更新，请重新打开记录")
    owner = db.get(ChatSession, case_id)
    record_event(db, user.tenant_id, owner.user_id, "case", case_id, "handoff_reviewed",
                 {"status": body.status, "actor": user.username})
    db.commit()
    return {"status": body.status}


class RetrievalIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    doc_ids: list[str] = Field(default_factory=list, max_length=20)
    include_official: bool = True


@router.post("/retrieval-test")
def retrieval_test(body: RetrievalIn, user: AuthUser = Depends(require_retrieval_allowance)):
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
            SupportCase.status.in_(["investigating", "needs_information", "awaiting_results", "evidence_submitted", "sample_recovered"]),
            func.coalesce(SupportCase.data["kind"].as_string(), "problem") == "problem",
        )
        .order_by(sql_case((SupportCase.severity == "P0", 0), (SupportCase.severity == "P1", 1),
                           (SupportCase.severity == "P2", 2), else_=3), SupportCase.updated_at.desc())
        .limit(20)
        .all()
    )
    items = [
        {
            "id": "case-" + c.session_id,
            "title": s.title,
            "state": {"investigating": "继续排查", "needs_information": "待补充证据", "awaiting_results": "待提交检查结果",
                      "evidence_submitted": "新结果待核对", "sample_recovered": "待确认实际结局"}[c.status],
            "url": "/issues/" + c.session_id,
            "kind": "case", "resource_id": c.session_id, "severity": c.severity,
            "priority": {"P0": 0, "P1": 10, "P2": 20, "P3": 30}.get(c.severity, 20),
            "action": "submit_result" if c.status == "awaiting_results" else "continue_case",
            "updated_at": c.updated_at.isoformat(),
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
            Document.created_by == user.user_id,
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
            "kind": "document", "resource_id": v.doc_id, "action": "retry_document", "priority": 15,
            "updated_at": v.created_at.isoformat(),
        }
        for v in versions
        if db.query(DocumentVersion.id).filter_by(doc_id=v.doc_id).order_by(DocumentVersion.number.desc()).first()[0] == v.id
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
    for r in runs:
        latest = db.query(AgentRun.id).filter_by(session_id=r.session_id, tenant_id=user.tenant_id).order_by(AgentRun.created_at.desc()).first()
        if latest[0] != r.id:
            continue
        personal = db.get(SupportCase, r.session_id)
        if personal and (personal.data.get("archived") or personal.data.get("saved") or personal.status == "user_resolved"):
            continue
        items = [item for item in items if item["id"] != "case-" + r.session_id]
        url = ("/support/" if personal else "/investigation/") + r.session_id
        if not personal:
            url += "?run=" + r.id
        if r.status == "interrupted" and personal:
            from urllib.parse import urlencode

            url += "?" + urlencode({"question": r.data.get("question", "请使用已保存的证据重新分析"), "workflow": "auto"})
        items.append({
            "id": "run-" + r.id,
            "title": r.data.get("question", "调查")[:80],
            "state": {"running": "正在分析", "cancel_requested": "正在停止", "interrupted": "运行曾被重启中断"}[
                r.status
            ],
            "url": url, "kind": "run", "resource_id": r.id,
            "action": "resume_analysis" if r.status == "interrupted" else "view_run",
            "priority": min(12, {"P0": 0, "P1": 10}.get(personal.severity, 12)) if r.status == "interrupted" and personal else 12 if r.status == "interrupted" else 80,
            "updated_at": r.updated_at.isoformat(),
        })
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
            "kind": "completed", "action": "view_run", "priority": 100,
            "updated_at": r.updated_at.isoformat(),
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
                "kind": "approval", "resource_id": r.id, "action": "review_approval", "priority": 16,
            }
            for r in approvals
        )
    items = sorted(items, key=lambda item: item["priority"])[:60]
    return {"items": items, "total": len(items), "scope": "按优先级展示最多60项记录；近期完成记录不计入待办"}


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
    runs = (
        db.query(AgentRun)
        .join(ChatSession, ChatSession.id == AgentRun.session_id)
        .outerjoin(SupportCase, SupportCase.session_id == AgentRun.session_id)
        .filter(
            AgentRun.tenant_id == user.tenant_id,
            ChatSession.tenant_id == user.tenant_id,
            ChatSession.deleted_at.is_(None),
            SupportCase.session_id.is_(None),
        )
    )
    if kind == "remediation":
        run = runs.filter(AgentRun.id == row.run_id).first()
    else:
        run = runs.filter(AgentRun.session_id == row.session_id).order_by(AgentRun.created_at.desc()).first()
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
