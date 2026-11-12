"""问题状态与归档分开；旧版saved只表示归档，不推断已解决。"""
from fastapi import HTTPException

from app.db.models import ChatSession, SupportCase

CASE_STATUSES = {"investigating", "needs_information", "awaiting_results", "evidence_submitted", "sample_recovered", "user_resolved"}


def is_archived(case):
    return bool(case.data.get("archived") or case.data.get("saved") or case.status == "saved")


def case_status(case):
    if case.status == "saved":
        verdict = case.data.get("report", {}).get("verification", {}).get("verdict") if case.data.get("report") else None
        return "sample_recovered" if verdict == "sample_recovered" else "investigating"
    return case.status


def assert_editable(case):
    if is_archived(case):
        raise HTTPException(409, "记录已归档，请在问题中心重新打开后继续")
    if case_status(case) == "user_resolved":
        raise HTTPException(409, "问题已由用户确认解决，请重新打开后继续排查")


def list_cases(db, user, site_id=None, kind=None, status=None, archived=None):
    query = db.query(SupportCase, ChatSession).join(ChatSession, SupportCase.session_id == ChatSession.id).filter(
        SupportCase.tenant_id == user.tenant_id, SupportCase.user_id == user.user_id, ChatSession.deleted_at.is_(None))
    if site_id:
        query = query.filter(SupportCase.site_id == site_id)
    rows = query.order_by(SupportCase.created_at.desc()).all()
    return [{"id": c.session_id, "title": s.title, "status": case_status(c), "site_id": c.site_id,
             "site_name": c.data.get("profile", {}).get("name", "未关联网站"),
             "kind": c.data.get("kind", "problem"), "archived": is_archived(c), "saved": is_archived(c),
             "problem": c.data.get("problem", {}), "solution": c.data.get("solution", ""),
             "summary": (c.data.get("report") or {}).get("summary", ""),
             "created_at": c.created_at.isoformat()}
            for c, s in rows
            if (not kind or c.data.get("kind", "problem") == kind)
            and (not status or case_status(c) == status)
            and (archived is None or is_archived(c) == archived)]


def reopen(case):
    case.status = "investigating"
    case.data = {**case.data, "archived": False, "saved": False, "user_confirmed_resolved": False}
