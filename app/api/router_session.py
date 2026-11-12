"""会话接口：创建 / 列表 / 详情（含消息）。"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import SessionIn
from app.auth.deps import AuthUser, get_current_user
from app.db.async_session import get_async_db
from app.db.models import Message, SupportCase
from app.services import chat_service

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class DeleteSessionsIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)


@router.post("/delete")
async def delete_sessions(body: DeleteSessionsIn, user: AuthUser = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    count = await db.run_sync(chat_service.delete_sessions, user.tenant_id, user.user_id, body.ids)
    await db.commit()
    return {"deleted": count}


@router.delete("/{session_id}")
async def delete_session(session_id: str, user: AuthUser = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    count = await db.run_sync(chat_service.delete_sessions, user.tenant_id, user.user_id, [session_id])
    await db.commit()
    return {"deleted": count}


@router.post("")
async def create_session(body: SessionIn, user: AuthUser = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    session = await db.run_sync(chat_service.create_session, user.tenant_id, user.user_id, body.title)
    return {"id": session.id, "title": session.title, "created_at": session.created_at.isoformat()}


@router.get("")
async def list_sessions(user: AuthUser = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    sessions = await db.run_sync(chat_service.list_sessions, user.tenant_id, user.user_id)
    personal = set((await db.scalars(select(SupportCase.session_id).filter_by(tenant_id=user.tenant_id, user_id=user.user_id))).all())
    sessions = [s for s in sessions if s.id not in personal]
    return [{"id": s.id, "title": s.title, "created_at": s.created_at.isoformat()} for s in sessions]


@router.get("/{session_id}")
async def get_session(session_id: str, user: AuthUser = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    session = await db.run_sync(chat_service.get_session, user.tenant_id, session_id, user.user_id)
    if not session:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    messages = (await db.scalars(select(Message).where(Message.session_id == session_id).order_by(Message.id.asc()))).all()
    return {
        "id": session.id,
        "title": session.title,
        "messages": [
            {"role": m.role, "content": m.content, "meta": m.meta, "created_at": m.created_at.isoformat()} for m in messages
        ],
    }
