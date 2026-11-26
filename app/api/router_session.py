"""会话接口：创建 / 列表 / 详情（含消息）。"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import SessionIn
from app.auth.deps import AuthUser, get_assistant_user, get_current_user
from app.container import get_container
from app.db.base import SessionLocal, get_db, run_with_sqlite_retry
from app.db.models import AgentRun, Message, SupportCase
from app.services import background_runs, chat_service
from app.services.assistant_quota import admit_chat, usage
from app.services.cache import check_rate_limit
from app.services.support_evidence import redact

router = APIRouter(prefix="/api/sessions", tags=["sessions"])
assistant_router = APIRouter(prefix="/api/assistant", tags=["assistant"])


class DeleteSessionsIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class RenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=100)


def session_summary(session):
    return {"id": session.id, "title": session.title, "created_at": session.created_at.isoformat()}


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
        return session_summary(chat_service.create_session(db, user.tenant_id, user.user_id, body.title))


@router.get("")
def list_sessions(user: AuthUser = Depends(get_current_user)):
    with SessionLocal() as db:
        sessions = chat_service.list_sessions(db, user.tenant_id, user.user_id)
        personal = set(
            db.scalars(select(SupportCase.session_id).filter_by(tenant_id=user.tenant_id, user_id=user.user_id)).all()
        )
        sessions = [s for s in sessions if s.id not in personal]
        return [session_summary(s) for s in sessions]


@router.get("/{session_id}")
def get_session(session_id: str, user: AuthUser = Depends(get_current_user)):
    with SessionLocal() as db:
        return session_history(db, owned_session(db, user, session_id))


def owned_session(db, user, session_id, *, assistant_only=False):
    session = chat_service.get_session(db, user.tenant_id, session_id, user.user_id)
    if session is None or (assistant_only and not session.assistant_chat):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return session


def session_history(db, session):
    active_run = db.scalar(
        select(AgentRun.id).where(
            AgentRun.session_id == session.id,
            AgentRun.tenant_id == session.tenant_id,
            AgentRun.status.in_(background_runs.ACTIVE),
            AgentRun.data["background"].as_boolean().is_(True),
        )
    )
    messages = db.scalars(select(Message).where(Message.session_id == session.id).order_by(Message.id.asc())).all()
    return {
        **session_summary(session),
        "active_run_id": active_run,
        "messages": [
            {"role": m.role, "content": m.content, "meta": m.meta, "created_at": m.created_at.isoformat()}
            for m in messages
        ],
    }


def rename(db, session, title):
    title = title.strip()
    if not title:
        raise HTTPException(422, "请输入对话名称")
    session.title = redact(title)["text"]
    db.commit()
    return session_summary(session)


@router.patch("/{session_id}")
def rename_session(session_id: str, body: RenameIn, user: AuthUser = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    return rename(db, owned_session(db, user, session_id), body.title)


@assistant_router.get("/usage")
def assistant_usage(user: AuthUser = Depends(get_assistant_user), db: Session = Depends(get_db)):
    return usage(db, user)


@assistant_router.get("/conversations")
def assistant_list(user: AuthUser = Depends(get_assistant_user), db: Session = Depends(get_db)):
    return [session_summary(s) for s in chat_service.list_sessions(db, user.tenant_id, user.user_id, assistant_chat=True)]


def limit_assistant_write(user):
    container = get_container()
    if not check_rate_limit(container.cache, f"assistant-write:{user.user_id}", container.settings.rate_limit_per_minute):
        raise HTTPException(429, "请求过于频繁，请稍后再试")


@assistant_router.post("/conversations", status_code=201)
def assistant_create(body: SessionIn, user: AuthUser = Depends(get_assistant_user), db: Session = Depends(get_db)):
    limit_assistant_write(user)
    return session_summary(chat_service.create_session(
        db, user.tenant_id, user.user_id, redact(body.title.strip())["text"] or "新对话", assistant_chat=True,
    ))


@assistant_router.get("/conversations/{session_id}")
def assistant_history(session_id: str, user: AuthUser = Depends(get_assistant_user), db: Session = Depends(get_db)):
    return session_history(db, owned_session(db, user, session_id, assistant_only=True))


@assistant_router.patch("/conversations/{session_id}")
def assistant_rename(session_id: str, body: RenameIn, user: AuthUser = Depends(get_assistant_user),
                     db: Session = Depends(get_db)):
    return rename(db, owned_session(db, user, session_id, assistant_only=True), body.title)


@assistant_router.delete("/conversations/{session_id}")
def assistant_delete(session_id: str, user: AuthUser = Depends(get_assistant_user), db: Session = Depends(get_db)):
    owned_session(db, user, session_id, assistant_only=True)
    count = chat_service.delete_sessions(db, user.tenant_id, user.user_id, [session_id])
    db.commit()
    return {"deleted": count}


class PersonalReplyIn(BaseModel):
    request_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    message: str = Field(min_length=1, max_length=8000)
    answer: str | None = Field(default=None, min_length=1, max_length=40000)


@assistant_router.post("/conversations/{session_id}/personal-turn")
def personal_turn(session_id: str, body: PersonalReplyIn, user: AuthUser = Depends(get_assistant_user)):
    """记录浏览器直连模型的消息。模型地址与凭据仅由浏览器持有。"""
    limit_assistant_write(user)

    def save_turn():
        with SessionLocal() as db:
            admit_chat(db, user, session_id)
            owned_session(db, user, session_id, assistant_only=True)
            user_key = f"personal:{session_id}:{body.request_id}:u"
            answer_key = f"personal:{session_id}:{body.request_id}:a"
            question = db.scalar(select(Message).filter_by(tenant_id=user.tenant_id, idempotency_key=user_key))
            answer = db.scalar(select(Message).filter_by(tenant_id=user.tenant_id, idempotency_key=answer_key))
            if question is None:
                if body.answer is not None or not body.message.strip():
                    raise HTTPException(409, "请先发送本轮问题")
                db.add(Message(tenant_id=user.tenant_id, session_id=session_id, role="user",
                               content=body.message, idempotency_key=user_key))
            elif question.content != body.message:
                raise HTTPException(409, "同一请求编号不能用于不同问题")
            if answer is None and body.answer is not None:
                db.add(Message(tenant_id=user.tenant_id, session_id=session_id, role="assistant",
                               content=body.answer, idempotency_key=answer_key, meta={"personal_model": True}))
            db.commit()
            return {"answer": answer.content if answer else body.answer}

    return run_with_sqlite_retry(save_turn)
