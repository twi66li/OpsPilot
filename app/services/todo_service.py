"""首页待办聚合：未结问题、失败入库、运行中/中断调查、近期完成与待审批。"""
from sqlalchemy import case as sql_case
from sqlalchemy import func

from app.db.models import AgentRun, ApprovalRequest, ChatSession, Document, DocumentVersion, SupportCase
from app.services.knowledge_access import personal_namespace


def todo_items(db, user):
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
