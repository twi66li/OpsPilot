"""维护案例记录：时间线、严重度与标签、备注评论与 Markdown 报告导出。"""

from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import AuthUser, get_current_user
from app.db.base import get_db
from app.db.models import ChatSession
from app.services.maintenance_report import case_report
from app.services.maintenance_timeline import build_case_timeline, record_event
from app.services.support_evidence import owned_case, redact

router = APIRouter()


@router.get("/cases/{case_id}/timeline")
def case_timeline(case_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    case = owned_case(db, user, case_id)
    return build_case_timeline(db, user, case)


class MetadataIn(BaseModel):
    severity: Literal["P0", "P1", "P2", "P3"] = "P2"
    labels: list[str] = Field(default_factory=list, max_length=8)


@router.put("/cases/{case_id}/metadata")
def metadata(case_id: str, body: MetadataIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    case = owned_case(db, user, case_id)
    labels = [redact(label[:30])["text"] for label in body.labels]
    case.severity, case.labels = body.severity, labels
    record_event(
        db,
        user.tenant_id,
        user.user_id,
        "case",
        case_id,
        "metadata_changed",
        {"severity": body.severity, "labels": labels, "actor": user.username},
    )
    db.commit()
    return {"severity": case.severity, "labels": case.labels}


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@router.post("/cases/{case_id}/comments")
def comment(case_id: str, body: CommentIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    owned_case(db, user, case_id)
    record_event(
        db,
        user.tenant_id,
        user.user_id,
        "case",
        case_id,
        "comment",
        {"text": redact(body.text)["text"], "actor": user.username},
    )
    db.commit()
    return {"saved": True}


@router.get("/cases/{case_id}/report")
def export_report(case_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    case = owned_case(db, user, case_id)
    title = db.get(ChatSession, case_id).title
    return Response(
        case_report(case, title, build_case_timeline(db, user, case)["events"]),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="opspilot-{case_id}.md"'},
    )
