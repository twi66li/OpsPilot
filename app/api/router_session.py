"""会话接口：创建 / 列表 / 详情（含消息）。"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.schemas import SessionIn
from app.auth.deps import AuthUser, get_current_user
from app.db.base import SessionLocal
from app.db.models import AgentRun, Message, SupportCase
from app.services import background_runs, chat_service

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class DeleteSessionsIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)


@router.post("/delete")
def delete_sessions(body: DeleteSessionsIn, user: AuthUser = Depends(get_current_user)):
    with SessionLocal() as db:
        count = chat_service.delete_sessions(db, user.tenant_id, user.user_id, body.ids)
        db.commit()
        return {"deleted": count}


@router.delete("/{session_id}")
def delete_session(session_id: str, user: AuthUser = Depends(get_current_user)):
    with SessionLocal() as db:
        count = chat_service.delete_sessions(db, user.tenant_id, user.user_id, [session_id])
        db.commit()
        return {"deleted": count}


@router.post("")
def create_session(body: SessionIn, user: AuthUser = Depends(get_current_user)):
    with SessionLocal() as db:
        session = chat_service.create_session(db, user.tenant_id, user.user_id, body.title)
        return {"id": session.id, "title": session.title, "created_at": session.created_at.isoformat()}


@router.get("")
def list_sessions(user: AuthUser = Depends(get_current_user)):
    with SessionLocal() as db:
        sessions = chat_service.list_sessions(db, user.tenant_id, user.user_id)
        personal = set(
            db.scalars(select(SupportCase.session_id).filter_by(tenant_id=user.tenant_id, user_id=user.user_id)).all()
        )
        sessions = [s for s in sessions if s.id not in personal]
        return [{"id": s.id, "title": s.title, "created_at": s.created_at.isoformat()} for s in sessions]


@router.get("/{session_id}")
def get_session(session_id: str, user: AuthUser = Depends(get_current_user)):
    with SessionLocal() as db:
        session = chat_service.get_session(db, user.tenant_id, session_id, user.user_id)
        if not session:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
        active_run = db.scalar(
            select(AgentRun.id).where(
                AgentRun.session_id == session_id,
                AgentRun.tenant_id == user.tenant_id,
                AgentRun.status.in_(background_runs.ACTIVE),
                AgentRun.data["background"].as_boolean().is_(True),
            )
        )
        messages = db.scalars(select(Message).where(Message.session_id == session_id).order_by(Message.id.asc())).all()
        return {
            "id": session.id,
            "title": session.title,
            "active_run_id": active_run,
            "messages": [
                {"role": m.role, "content": m.content, "meta": m.meta, "created_at": m.created_at.isoformat()}
                for m in messages
            ],
        }
