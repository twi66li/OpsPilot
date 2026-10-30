"""工单与审批单服务。"""
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ApprovalRequest, Ticket


def create_ticket(db: Session, tenant_id: str, session_id: str, draft: dict, created_by: str, approver: str = "", approval_id: str | None = None, comment: str = "") -> Ticket:
    if approval_id:
        existing = db.query(Ticket).filter(Ticket.approval_id == approval_id, Ticket.tenant_id == tenant_id).first()
        if existing:
            return existing
    ticket = Ticket(
        tenant_id=tenant_id,
        session_id=session_id,
        approval_id=approval_id,
        title=draft.get("title", "未命名故障工单"),
        description=draft.get("description", ""),
        severity=draft.get("severity", "P2"),
        status="open",
        created_by=created_by,
        approver=approver,
        decision_comment=comment,
        decided_at=datetime.now(timezone.utc),
    )
    db.add(ticket)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if approval_id:
            existing = db.query(Ticket).filter(Ticket.approval_id == approval_id, Ticket.tenant_id == tenant_id).first()
            if existing:
                return existing
        raise
    return ticket


def list_tickets(db: Session, tenant_id: str) -> list[Ticket]:
    return db.query(Ticket).filter(Ticket.tenant_id == tenant_id).order_by(Ticket.created_at.desc()).limit(100).all()


def get_pending_approval(db: Session, tenant_id: str, session_id: str) -> ApprovalRequest | None:
    return (
        db.query(ApprovalRequest)
        .filter(ApprovalRequest.tenant_id == tenant_id, ApprovalRequest.session_id == session_id, ApprovalRequest.status == "pending")
        .first()
    )


def get_or_create_approval(db: Session, tenant_id: str, session_id: str, draft: dict, requested_by: str) -> ApprovalRequest:
    existing = get_pending_approval(db, tenant_id, session_id)
    if existing:
        return existing
    req = ApprovalRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        ticket_draft=draft,
        status="pending",
        requested_by=requested_by,
    )
    db.add(req)
    db.commit()
    return req
