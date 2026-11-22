"""排障案例生命周期路由：创建、查询、问题更新、状态流转、归档与删除。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import AuthUser, get_current_user
from app.db.base import get_db
from app.db.models import CaseHandoff
from app.services import chat_service
from app.services.case_service import (
    CASE_STATUSES,
    assert_editable,
    create_case_record,
    is_archived,
    list_cases,
    reopen,
)
from app.services.maintenance_timeline import record_event
from app.services.support_evidence import CaseIn, owned_case, owned_site, redact
from app.services.support_report import case_dict

router = APIRouter()


@router.post("/cases")
def create_case(body: CaseIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    site = owned_site(db, user, body.site_id) if body.site_id else None
    problem = {
        key: redact(getattr(body, key))["text"]
        for key in ("symptom", "endpoint", "impact", "started_at", "recent_changes")
    }
    case = create_case_record(
        db,
        user.tenant_id,
        user.user_id,
        redact(body.title)["text"],
        problem,
        actor=user.username,
        site=site,
        kind=body.kind,
    )
    db.commit()
    return case_dict(case)


@router.get("/cases")
def cases(
    site_id: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    archived: bool | None = None,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return list_cases(db, user, site_id, kind, status, archived)


class ProblemIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    symptom: str = Field(default="", max_length=2000)
    endpoint: str = Field(default="", max_length=200)
    impact: str = Field(default="", max_length=500)
    started_at: str = Field(default="", max_length=100)
    recent_changes: str = Field(default="", max_length=1000)


@router.put("/cases/{session_id}/problem")
def update_problem(
    session_id: str, body: ProblemIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)
):
    case = owned_case(db, user, session_id)
    assert_editable(case)
    if body.title is not None:
        session = chat_service.get_session(db, user.tenant_id, session_id, user.user_id)
        session.title = redact(body.title)["text"]
    case.data = {
        **case.data,
        "kind": "problem",
        "problem": {k: redact(v)["text"] for k, v in body.model_dump(exclude={"title"}).items()},
    }
    record_event(
        db,
        user.tenant_id,
        user.user_id,
        "case",
        session_id,
        "problem_updated",
        {"problem": case.data.get("problem"), "actor": user.username},
    )
    db.commit()
    return case_dict(case)


class StatusIn(BaseModel):
    status: str
    note: str = Field(default="", max_length=2000)
    user_confirmed: bool = False


@router.post("/cases/{session_id}/status")
def change_status(
    session_id: str, body: StatusIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)
):
    case = owned_case(db, user, session_id)
    if body.status == "reopen":
        reopen(case)
    else:
        assert_editable(case)
        if body.status not in CASE_STATUSES:
            raise HTTPException(400, "未知的问题状态")
        if body.status == "user_resolved" and (not body.user_confirmed or not body.note.strip()):
            raise HTTPException(400, "确认解决需要本人确认并填写实际处理记录")
        if body.status == "sample_recovered":
            raise HTTPException(400, "样本核对状态由程序计算，不能手动指定")
        case.status = body.status
    case.data = {**case.data, "status_note": redact(body.note)["text"]}
    if body.status == "user_resolved":
        case.data = {**case.data, "solution": redact(body.note)["text"], "user_confirmed_resolved": True}
    record_event(
        db,
        user.tenant_id,
        user.user_id,
        "case",
        session_id,
        "status_changed",
        {"status": case.status, "note": case.data.get("status_note"), "actor": user.username},
    )
    db.commit()
    return case_dict(case)


@router.get("/cases/{session_id}")
def get_case(session_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    case = owned_case(db, user, session_id)
    result = case_dict(case)
    handoff = db.get(CaseHandoff, session_id)
    if handoff:
        result["handoff"] = {
            key: handoff.data.get(key) for key in ("recipient", "note", "shared_at", "review_note", "reviewed_at")
        } | {"status": handoff.status}
    return result


@router.delete("/cases/{session_id}")
def delete_case(session_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    owned_case(db, user, session_id)
    count = chat_service.delete_sessions(db, user.tenant_id, user.user_id, [session_id])
    db.commit()
    return {"deleted": count}


class SaveIn(BaseModel):
    solution: str = Field(min_length=1, max_length=2000)
    user_confirmed: bool = False


@router.post("/cases/{session_id}/save")
def save_case(session_id: str, body: SaveIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    case = owned_case(db, user, session_id)
    if is_archived(case):
        return case_dict(case)
    if not body.user_confirmed:
        raise HTTPException(400, "保存案例需要本人确认处理记录")
    if not case.data.get("report"):
        raise HTTPException(409, "请先完成一次排障分析")
    case.data = {
        **case.data,
        "saved": True,
        "archived": True,
        "solution": redact(body.solution)["text"],
        "result_label": "用户确认的处理记录；恢复结论以报告中的证据范围为限",
    }
    record_event(
        db,
        user.tenant_id,
        user.user_id,
        "case",
        session_id,
        "case_archived",
        {"solution": case.data.get("solution"), "actor": user.username},
    )
    db.commit()
    return case_dict(case)
