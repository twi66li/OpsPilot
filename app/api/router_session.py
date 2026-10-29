"""会话接口：创建 / 列表 / 详情（含消息）。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.schemas import SessionIn
from app.auth.deps import AuthUser, get_current_user
from app.db.base import get_db
from app.db.models import Message
from app.services import chat_service

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.post("")
def create_session(body: SessionIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    session = chat_service.create_session(db, user.tenant_id, user.user_id, body.title)
    return {"id": session.id, "title": session.title, "created_at": session.created_at.isoformat()}


@router.get("")
def list_sessions(user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    sessions = chat_service.list_sessions(db, user.tenant_id)
    return [{"id": s.id, "title": s.title, "created_at": s.created_at.isoformat()} for s in sessions]


@router.get("/{session_id}")
def get_session(session_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    session = chat_service.get_session(db, user.tenant_id, session_id)
    if not session:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    messages = db.query(Message).filter(Message.session_id == session_id).order_by(Message.id.asc()).all()
    return {
        "id": session.id,
        "title": session.title,
        "messages": [
            {"role": m.role, "content": m.content, "meta": m.meta, "created_at": m.created_at.isoformat()} for m in messages
        ],
    }
