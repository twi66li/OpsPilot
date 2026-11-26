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
from app.services.case_service import assert_editable, case_status, create_case_record, is_archived
from app.services.maintenance_timeline import record_event
from app.services.support_evidence import EvidenceIn, new_evidence, owned_case, owned_site, redact
from app.services.support_report import case_dict

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


def _active_run_id(db: Session, tenant_id: str, target_session_id: str) -> str | None:
    return db.scalar(
        select(AgentRun.id).where(
            AgentRun.session_id == target_session_id,
            AgentRun.tenant_id == tenant_id,
            AgentRun.status.in_(background_runs.ACTIVE),
            AgentRun.data["background"].as_boolean().is_(True),
        )
    )


@assistant_router.get("/conversations/{session_id}")
def assistant_history(session_id: str, user: AuthUser = Depends(get_assistant_user), db: Session = Depends(get_db)):
    data = session_history(db, owned_session(db, user, session_id, assistant_only=True))
    case_ids = {
        m["meta"].get("case_id")
        for m in data["messages"]
        if isinstance(m.get("meta"), dict) and m["meta"].get("case_id")
    }
    cases = {}
    for cid in case_ids:
        case = db.query(SupportCase).filter_by(session_id=cid, tenant_id=user.tenant_id, user_id=user.user_id).first()
        if case is not None:
            cases[cid] = {**case_dict(case), "active_run_id": _active_run_id(db, user.tenant_id, cid)}
    data["cases"] = cases
    return data


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


class AssistantDiagnoseIn(BaseModel):
    request_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    text: str = Field(min_length=1, max_length=12000)
    label: str = Field(default="日志片段", min_length=1, max_length=100)
    endpoint: str = Field(default="", max_length=200)
    collected_at: str = Field(default="", max_length=80)
    question: str = Field(default="", max_length=2000)
    site_id: str | None = None
    case_id: str | None = None
    step_index: int | None = Field(default=None, ge=0, le=2)


@assistant_router.post("/conversations/{session_id}/diagnose")
def assistant_diagnose(session_id: str, body: AssistantDiagnoseIn, user: AuthUser = Depends(get_current_user)):
    """在当前聊天内按需发起或续接证据化诊断，服务端自动脱敏并关联排查卡片。"""
    limit_assistant_write(user)

    def prepare():
        with SessionLocal() as db:
            admit_chat(db, user, session_id)
            chat_sess = owned_session(db, user, session_id, assistant_only=True)
            user_key = f"diagnose:{session_id}:{body.request_id}:u"
            card_key = f"diagnose:{session_id}:{body.request_id}:card"
            existing_card = db.scalar(select(Message).filter_by(tenant_id=user.tenant_id, idempotency_key=card_key))
            if existing_card is not None:
                case = owned_case(db, user, existing_card.meta["case_id"])
                return {
                    "case": case_dict(case),
                    "workflow": existing_card.meta.get("workflow", "diagnose"),
                    "prompt": existing_card.meta.get("prompt", "请结合日志证据定位异常原因。"),
                    "redaction": existing_card.meta.get("redaction", {"replacements": 0}),
                }

            prior_msgs = db.scalars(
                select(Message).where(Message.session_id == session_id).order_by(Message.id.asc())
            ).all()
            case = None
            if body.case_id:
                case = owned_case(db, user, body.case_id)
                assert_editable(case)
            else:
                for msg in reversed(prior_msgs):
                    cid = (msg.meta or {}).get("case_id") if isinstance(msg.meta, dict) else None
                    if cid:
                        candidate = (
                            db.query(SupportCase)
                            .filter_by(session_id=cid, tenant_id=user.tenant_id, user_id=user.user_id)
                            .first()
                        )
                        if candidate and not is_archived(candidate) and case_status(candidate) != "user_resolved":
                            case = candidate
                            break
            if case is None:
                site = owned_site(db, user, body.site_id) if body.site_id else None
                context_lines = [
                    f"{m.role}: {m.content[:400]}"
                    for m in prior_msgs[-6:]
                    if not (isinstance(m.meta, dict) and m.meta.get("case_id"))
                ]
                if body.question.strip():
                    context_lines.append(body.question.strip())
                symptom = redact("\n".join(context_lines))["text"][:2000]
                case = create_case_record(
                    db,
                    user.tenant_id,
                    user.user_id,
                    (chat_sess.title or "故障诊断")[:100],
                    {
                        "symptom": symptom,
                        "endpoint": redact(body.endpoint)["text"],
                        "impact": "",
                        "started_at": "",
                        "recent_changes": "",
                    },
                    actor=user.username,
                    site=site,
                    kind="problem",
                )

            evidence = case.data.get("evidence", [])
            if len(evidence) >= 12 or sum(len(e["text"]) for e in evidence) + len(body.text) > 30000:
                raise HTTPException(400, "本诊断记录证据已达上限，请开启新对话")
            plan = case.data.get("plan")
            kind = "check_result" if plan else "initial"
            workflow = "review" if plan else "diagnose"
            if plan and body.step_index is not None and body.step_index >= len(plan["steps"]):
                raise HTTPException(400, "检查步骤不存在")
            ev_in = EvidenceIn(
                text=body.text,
                label=body.label,
                kind=kind,
                collected_at=body.collected_at,
                endpoint=body.endpoint,
                plan_revision=plan["revision"] if plan else None,
                step_index=body.step_index if plan else None,
            )
            redacted = redact(body.text)
            item = new_evidence(ev_in)
            case.data = {**case.data, "kind": "problem", "evidence": [*evidence, item]}
            case.status = "evidence_submitted"
            record_event(
                db,
                user.tenant_id,
                user.user_id,
                "case",
                case.session_id,
                "evidence_submitted",
                {"evidence_id": item["id"], "label": item["label"], "kind": item["kind"]},
            )
            clean_q = redact(body.question.strip())["text"] if body.question.strip() else ""
            prompt = clean_q or (
                "已提交检查后的新结果，请结合已确认方案核对原异常是否消失及仍缺少的信息。"
                if workflow == "review"
                else "请结合对话上下文与提交的日志证据定位异常原因，并给出下一步检查方案。"
            )
            user_text = f"提交证据：{item['label']}" + (f"\n{clean_q}" if clean_q else "")
            redaction_meta = {
                "replacements": redacted["replacements"],
                "findings": redacted["findings"],
                "review_findings": redacted["review_findings"],
            }
            db.add(
                Message(
                    tenant_id=user.tenant_id,
                    session_id=session_id,
                    role="user",
                    content=user_text,
                    idempotency_key=user_key,
                )
            )
            db.add(
                Message(
                    tenant_id=user.tenant_id,
                    session_id=session_id,
                    role="assistant",
                    content=item["label"],
                    idempotency_key=card_key,
                    meta={
                        "case_id": case.session_id,
                        "evidence_id": item["id"],
                        "workflow": workflow,
                        "prompt": prompt,
                        "redaction": redaction_meta,
                    },
                )
            )
            db.commit()
            return {"case": case_dict(case), "workflow": workflow, "prompt": prompt, "redaction": redaction_meta}

    return run_with_sqlite_retry(prepare)
