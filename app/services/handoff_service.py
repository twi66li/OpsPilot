"""案例交接与成员复核服务。"""

from fastapi import HTTPException
from sqlalchemy import update

from app.db.models import CaseHandoff, ChatSession, SupportCase, User, _now, _uuid
from app.services.evidence_checks import analyze_samples
from app.services.maintenance_timeline import record_event
from app.services.support_evidence import owned_case, redact


def _received_handoffs_query(db, user):
    return db.query(CaseHandoff).join(ChatSession, ChatSession.id == CaseHandoff.case_id).filter(
        CaseHandoff.tenant_id == user.tenant_id,
        CaseHandoff.recipient_id == user.user_id,
        ChatSession.tenant_id == user.tenant_id,
        ChatSession.deleted_at.is_(None),
    )


def assign_case_handoff(db, user, case_id, recipient_name, note):
    case = owned_case(db, user, case_id)
    if case.data.get("kind", "problem") != "problem" or not note.strip():
        raise HTTPException(400, "请选择问题并填写交接备注")
    recipient = db.query(User).filter_by(tenant_id=user.tenant_id, username=recipient_name.strip()).first()
    if not recipient or recipient.id == user.user_id:
        raise HTTPException(404, "请填写本工作区内另一位成员的用户名")
    report = case.data.get("report") or {}
    snapshot = {
        "title": db.get(ChatSession, case_id).title,
        "site_name": case.data.get("profile", {}).get("name"),
        "problem": {key: case.data.get("problem", {}).get(key) for key in ("symptom", "endpoint", "started_at")},
        "summary": redact(report.get("summary", ""))["text"],
        "evidence": [
            {key: item.get(key) for key in ("id", "label", "kind", "collected_at", "source_kind")}
            | {"text": redact(item["text"])["text"]}
            for item in case.data.get("evidence", [])
        ],
        "verification": analyze_samples(case.data)["verification"],
    }
    db.get(SupportCase, case_id, with_for_update=True)
    handoff = db.get(CaseHandoff, case_id, with_for_update=True) or CaseHandoff(case_id=case_id, tenant_id=user.tenant_id)
    handoff.version, handoff.status, handoff.recipient_id = _uuid(), "pending", recipient.id
    handoff.data = {
        "recipient": recipient.username,
        "sender": user.username,
        "note": redact(note.strip())["text"],
        "shared_at": _now().isoformat() + "Z",
        "snapshot": snapshot,
    }
    db.add(handoff)
    record_event(
        db,
        user.tenant_id,
        user.user_id,
        "case",
        case_id,
        "handoff_assigned",
        {"recipient": recipient.username, "actor": user.username},
    )
    db.commit()
    return {key: value for key, value in handoff.data.items() if key != "snapshot"} | {"status": handoff.status}


def revoke_case_handoff(db, user, case_id):
    owned_case(db, user, case_id)
    handoff = db.get(CaseHandoff, case_id, with_for_update=True)
    if handoff:
        db.delete(handoff)
        record_event(db, user.tenant_id, user.user_id, "case", case_id, "handoff_revoked", {"actor": user.username})
        db.commit()
    return {"revoked": True}


def list_received_handoffs(db, user):
    rows = _received_handoffs_query(db, user).order_by(CaseHandoff.data["shared_at"].as_string().desc()).limit(30)
    return {"items": [{"case_id": row.case_id, "version": row.version, "status": row.status, **row.data} for row in rows]}


def review_case_handoff(db, user, case_id, version, status, note):
    row = _received_handoffs_query(db, user).filter(CaseHandoff.case_id == case_id).first()
    if not row:
        raise HTTPException(404, "交接记录不存在或已撤回")
    if not note.strip():
        raise HTTPException(400, "请填写复核意见")
    redacted_note = redact(note.strip())["text"]
    changed = db.execute(
        update(CaseHandoff)
        .where(
            CaseHandoff.case_id == case_id,
            CaseHandoff.tenant_id == user.tenant_id,
            CaseHandoff.recipient_id == user.user_id,
            CaseHandoff.version == version,
            CaseHandoff.status == "pending",
        )
        .values(status=status, data={**row.data, "review_note": redacted_note, "reviewed_at": _now().isoformat() + "Z"})
    )
    if changed.rowcount != 1:
        raise HTTPException(409, "交接状态已更新，请重新打开记录")
    owner = db.get(ChatSession, case_id)
    record_event(
        db,
        user.tenant_id,
        owner.user_id,
        "case",
        case_id,
        "handoff_reviewed",
        {"status": status, "actor": user.username},
    )
    db.commit()
    return {"status": status}
