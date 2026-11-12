"""会话与消息持久化。"""
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models import AgentRun, ApprovalRequest, ChatSession, Message, RemediationRequest, _now


def create_session(db: Session, tenant_id: str, user_id: str, title: str = "新会话") -> ChatSession:
    session = ChatSession(tenant_id=tenant_id, user_id=user_id, title=title[:200])
    db.add(session)
    db.commit()
    return session


def get_session(db: Session, tenant_id: str, session_id: str, user_id: str) -> ChatSession | None:
    """个人会话同时校验租户与所有者；团队工单另按角色授权。"""
    return db.query(ChatSession).filter(
        ChatSession.id == session_id, ChatSession.tenant_id == tenant_id, ChatSession.user_id == user_id,
        ChatSession.deleted_at.is_(None),
    ).first()


def list_sessions(db: Session, tenant_id: str, user_id: str) -> list[ChatSession]:
    query = db.query(ChatSession).filter(ChatSession.tenant_id == tenant_id, ChatSession.user_id == user_id,
                                        ChatSession.deleted_at.is_(None))
    return query.order_by(ChatSession.created_at.desc()).limit(100).all()


def delete_sessions(db: Session, tenant_id: str, user_id: str, ids: list[str]) -> int:
    """移出个人工作区并阻断访问，保留关联审批、工单和检查点审计。"""
    rows = db.query(ChatSession).filter(ChatSession.id.in_(ids), ChatSession.tenant_id == tenant_id,
                                        ChatSession.user_id == user_id, ChatSession.deleted_at.is_(None)).all()
    if {row.id for row in rows} != set(ids):
        raise HTTPException(404, "部分会话不存在或不属于当前账号")
    for row in rows:
        row.deleted_at = _now()
    db.query(ApprovalRequest).filter(ApprovalRequest.session_id.in_(ids), ApprovalRequest.tenant_id == tenant_id,
        ApprovalRequest.status == "pending").update({"status":"cancelled", "comment":"会话已由所有者删除，取消待审批动作",
            "decided_at":_now()}, synchronize_session=False)
    run_ids = db.query(AgentRun.id).filter(AgentRun.session_id.in_(ids), AgentRun.tenant_id == tenant_id)
    db.query(RemediationRequest).filter(RemediationRequest.run_id.in_(run_ids), RemediationRequest.tenant_id == tenant_id,
        RemediationRequest.status == "pending").update({"status":"cancelled", "comment":"会话已由所有者删除，取消待审批动作",
            "decided_at":_now()}, synchronize_session=False)
    db.flush()
    return len(rows)


def add_message(db: Session, tenant_id: str, session_id: str, role: str, content: str, meta: dict | None = None) -> Message:
    msg = Message(tenant_id=tenant_id, session_id=session_id, role=role, content=content, meta=meta or {})
    db.add(msg)
    db.commit()
    return msg


def get_history(db: Session, session_id: str, limit: int = 10) -> list[dict]:
    rows = (
        db.query(Message)
        .filter(Message.session_id == session_id, Message.role.in_(["user", "assistant"]))
        .order_by(Message.id.desc())
        .limit(limit)
        .all()
    )
    return [{"role": r.role, "content": r.content} for r in reversed(rows)]
