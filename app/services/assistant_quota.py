"""按北京时间统计新对话、文档上传和平台模型用量。"""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select, update

from app.db.base import SessionLocal, run_with_sqlite_retry
from app.db.budget_models import BudgetLedgerState
from app.db.models import ChatSession, DocumentVersion, Message, User, _now
from app.services import chat_service
from app.services.knowledge_access import personal_namespace
from app.services.visitor_service import VISITOR_TENANT

_CST = timezone(timedelta(hours=8))
_model_subject = ContextVar("assistant_model_subject", default=None)
VISITOR_TOKEN_LIMIT = 50_000
PUBLIC_TOKEN_LIMIT = 500_000
UPLOAD_LIMIT = 10


def day_window():
    local = datetime.now(_CST).replace(hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc).replace(tzinfo=None), local + timedelta(days=1)


def _day_scope(user_id, until):
    return f"assistant:{(until - timedelta(days=1)).date()}:{user_id}"


def _chat_limit(user):
    return 3 if user.is_guest else 20


def _token_limit(tenant_id):
    return VISITOR_TOKEN_LIMIT if tenant_id == VISITOR_TENANT else 10_000_000


def _new_chats(db, user_id, since):
    return db.query(ChatSession.id).filter(ChatSession.user_id == user_id, ChatSession.first_turn_at >= since).count()


def _uploads(db, namespace, since):
    return db.query(DocumentVersion.id).filter(
        DocumentVersion.tenant_id == namespace, DocumentVersion.created_at >= since
    ).count()


def usage(db, user):
    since, until = day_window()
    data = {"resets_at": until.isoformat(), "chats": None, "tokens": None, "uploads": None}
    if user.role == "admin":
        return data
    ledger = db.get(BudgetLedgerState, _day_scope(user.user_id, until))
    spent = ledger.actual_tokens + ledger.unknown_tokens + ledger.in_flight_tokens if ledger else 0
    data["chats"] = {"used": _new_chats(db, user.user_id, since), "limit": _chat_limit(user)}
    data["tokens"] = {"used": spent, "limit": _token_limit(user.tenant_id)}
    if not user.is_guest:
        data["uploads"] = {"used": _uploads(db, personal_namespace(user.user_id), since), "limit": UPLOAD_LIMIT}
    return data


def check_upload(db, user):
    if user.role != "admin" and user.tenant_id == personal_namespace(user.user_id):
        since, _ = day_window()
        if _uploads(db, user.tenant_id, since) >= UPLOAD_LIMIT:
            raise HTTPException(429, "今天已提交 10 份文档，北京时间零点恢复额度")


def admit_chat(db, user, session_id):
    # 同一用户的首轮发送串行登记，删除会话也保留本日已用额度。
    db.execute(update(User).where(User.id == user.user_id).values(id=User.id))
    session = chat_service.get_session(db, user.tenant_id, session_id, user.user_id)
    if session is None:
        raise HTTPException(404, "会话不存在")
    if session.first_turn_at is not None:
        return
    has_history = db.scalar(select(Message.id).where(Message.session_id == session_id).limit(1))
    if has_history is not None:
        return
    since, _ = day_window()
    if user.role != "admin" and _new_chats(db, user.user_id, since) >= _chat_limit(user):
        raise HTTPException(429, f"今天已开启 {_chat_limit(user)} 个新对话，北京时间零点恢复额度")
    session.first_turn_at = _now()


def claim_chat(user, session_id):
    def claim():
        with SessionLocal() as db:
            admit_chat(db, user, session_id)
            db.commit()

    run_with_sqlite_retry(claim)


@contextmanager
def model_allowance(user_id, tenant_id, role):
    token = _model_subject.set((user_id, tenant_id) if role != "admin" else None)
    try:
        yield
    finally:
        _model_subject.reset(token)


def model_ledgers(global_ledger):
    subject = _model_subject.get()
    if subject is None:
        return []
    from app.services.budget_pg import DatabaseBudgetLedger

    user_id, tenant_id = subject
    _, until = day_window()
    scopes = [(user_id, _token_limit(tenant_id))]
    if tenant_id == VISITOR_TENANT:
        scopes.append(("all-visitors", PUBLIC_TOKEN_LIMIT))
    return [
        DatabaseBudgetLedger(scope=_day_scope(identity, until), limit=limit, model_limits=global_ledger.model_limits)
        for identity, limit in scopes
    ]
