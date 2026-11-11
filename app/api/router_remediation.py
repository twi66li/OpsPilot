"""修复申请→角色审批→固定演练动作→实际请求验证，模型没有直接执行权限。"""
from datetime import datetime, timezone
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.auth.deps import AuthUser, get_current_user, require_roles
from app.config import get_settings
from app.db.async_session import get_async_db
from app.db.base import get_db
from app.db.models import RemediationRequest
from app.services import remediation_service, run_service

router = APIRouter(prefix="/api/remediations", tags=["remediation"])


class RemediationIn(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)
    action: Literal["reset_demo_fault"] = "reset_demo_fault"
    reason: str = Field(min_length=1, max_length=600)


class DecisionIn(BaseModel):
    approved: bool
    comment: str = Field(default="", max_length=600)


def _serialize(row):
    return {"id": row.id, "run_id": row.run_id, "action": row.action, "action_label": remediation_service.ACTION_LABEL,
            "service": row.service, "reason": row.reason, "status": row.status, "requested_by": row.requested_by,
            "decided_by": row.decided_by, "comment": row.comment, "result": row.result,
            "created_at": row.created_at.isoformat(), "decided_at": row.decided_at.isoformat() if row.decided_at else None}


def _require_enabled(user):
    if not get_settings().remediation_enabled:
        raise HTTPException(503, "当前环境尚未启用演练修复")
    if user.tenant_id != "default":
        raise HTTPException(404, "本租户没有配置演练修复目标")


@router.get("/actions")
async def actions(user: AuthUser = Depends(get_current_user)):
    enabled = get_settings().remediation_enabled and user.tenant_id == "default"
    return {"enabled": enabled, "actions": [{"id": remediation_service.ACTION, "label": remediation_service.ACTION_LABEL,
                                            "environment": "test", "requires_approval": True}] if enabled else []}


@router.get("")
async def list_requests(user: AuthUser = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    rows = (await db.scalars(select(RemediationRequest).filter_by(tenant_id=user.tenant_id).order_by(
        RemediationRequest.created_at.desc()).limit(100))).all()
    return [_serialize(row) for row in rows]


@router.post("", status_code=201)
def create_request(body: RemediationIn, user: AuthUser = Depends(require_roles("admin", "engineer")),
                   db: Session = Depends(get_db)):
    _require_enabled(user)
    run = run_service.get_run(db, user.tenant_id, body.run_id, user.user_id)
    if run is None:
        raise HTTPException(404, "调查记录不存在")
    if run.status == "running":
        raise HTTPException(409, "请等待调查完成，再提出修复申请")
    row = RemediationRequest(tenant_id=user.tenant_id, run_id=run.id, action=body.action,
                             reason=body.reason, requested_by=user.username)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.query(RemediationRequest).filter_by(tenant_id=user.tenant_id, run_id=run.id, action=body.action).one()
    return _serialize(row)


@router.post("/{request_id}/decide")
def decide(request_id: str, body: DecisionIn, user: AuthUser = Depends(require_roles("admin", "engineer")),
           db: Session = Depends(get_db)):
    _require_enabled(user)
    row = db.query(RemediationRequest).filter_by(id=request_id, tenant_id=user.tenant_id).first()
    if row is None:
        raise HTTPException(404, "修复申请不存在")
    if row.status != "pending":
        if row.approved != body.approved:
            raise HTTPException(409, "审批决定已确定，不能修改")
        return _serialize(row)
    changed = db.execute(update(RemediationRequest).where(RemediationRequest.id == request_id,
                        RemediationRequest.status == "pending").values(
                            approved=body.approved, status="running" if body.approved else "rejected",
                            decided_by=user.username, comment=body.comment, decided_at=datetime.now(timezone.utc))).rowcount
    db.commit()
    db.refresh(row)
    if changed != 1:
        if row.approved != body.approved:
            raise HTTPException(409, "审批已由其他请求处理")
        return _serialize(row)
    if not body.approved:
        return _serialize(row)
    result = {"environment": "test", "source": "actual_test_requests", "probe_count_each": 3,
              "verification_scope": "短请求探针，不代表长期SLO；关闭演练注入不等同于生产故障根因修复"}
    try:
        succeeded = remediation_service.execute_demo_reset(get_settings().logs_url, result)
        row.status = "succeeded" if succeeded else "failed"
    except Exception as exc:
        row.status = "failed"
        result["error_type"] = type(exc).__name__
        result["message"] = "执行或验证未完成，请检查演练服务后重新调查；不自动重复执行"
    row.result = result
    db.commit()
    structlog.get_logger(__name__).info("remediation_finished", remediation_id=row.id, tenant_id=row.tenant_id,
                                      decided_by=user.username, status=row.status)
    with run_service.capture_run(run_service.RunRecorder(row.run_id, row.tenant_id)):
        run_service.emit("remediation", {"id": row.id, "status": row.status, "decided_by": row.decided_by})
    return _serialize(row)
