"""会话与消息持久化。"""
from sqlalchemy.orm import Session

from app.db.models import ChatSession, Message


def create_session(db: Session, tenant_id: str, user_id: str, title: str = "新会话") -> ChatSession:
    session = ChatSession(tenant_id=tenant_id, user_id=user_id, title=title[:200])
    db.add(session)
    db.commit()
    return session


def get_session(db: Session, tenant_id: str, session_id: str) -> ChatSession | None:
    """按租户过滤获取会话（跨租户不可见）。"""
    return db.query(ChatSession).filter(ChatSession.id == session_id, ChatSession.tenant_id == tenant_id).first()


def list_sessions(db: Session, tenant_id: str, user_id: str | None = None) -> list[ChatSession]:
    query = db.query(ChatSession).filter(ChatSession.tenant_id == tenant_id)
    if user_id:
        query = query.filter(ChatSession.user_id == user_id)
    return query.order_by(ChatSession.created_at.desc()).limit(100).all()


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
