"""案例交接复核与组织审批/工单详情路由。"""

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import AuthUser, get_current_user
from app.db.base import get_db
from app.services.handoff_service import (
    assign_case_handoff,
    list_received_handoffs,
    review_case_handoff,
    revoke_case_handoff,
)
from app.services.maintenance_workspace import get_operation_detail

router = APIRouter()


class HandoffIn(BaseModel):
    recipient: str = Field(min_length=1, max_length=64)
    note: str = Field(min_length=1, max_length=2000)


@router.put("/cases/{case_id}/handoff")
def assign_handoff(
    case_id: str, body: HandoffIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)
):
    return assign_case_handoff(db, user, case_id, body.recipient, body.note)


@router.delete("/cases/{case_id}/handoff")
def revoke_handoff(case_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return revoke_case_handoff(db, user, case_id)


@router.get("/handoffs")
def received_handoffs(user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return list_received_handoffs(db, user)


class HandoffReviewIn(BaseModel):
    version: str = Field(min_length=1, max_length=64)
    status: Literal["reviewed", "changes_requested"]
    note: str = Field(min_length=1, max_length=2000)


@router.post("/handoffs/{case_id}/review")
def review_handoff(
    case_id: str, body: HandoffReviewIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)
):
    return review_case_handoff(db, user, case_id, body.version, body.status, body.note)


@router.get("/operations/{record_id}")
def operation_detail(record_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return get_operation_detail(db, user, record_id)
