"""工单 / 故障记录 / 服务指标接口。"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.schemas import FaultIn
from app.auth.deps import AuthUser, get_current_user, require_roles
from app.db.async_session import get_async_db
from app.db.models import FaultRecord
from app.services import ticket_service
from app.services.metrics_service import query_service_metrics

router = APIRouter(tags=["ops"])


@router.get("/api/tickets")
async def list_tickets(user: AuthUser = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    tickets = await db.run_sync(ticket_service.list_tickets, user.tenant_id)
    return [
        {
            "id": t.id,
            "title": t.title,
            "severity": t.severity,
            "status": t.status,
            "created_by": t.created_by,
            "approver": t.approver,
            "session_id": t.session_id,
            "created_at": t.created_at.isoformat(),
        }
        for t in tickets
    ]


@router.get("/api/tickets/{ticket_id}")
async def get_ticket(ticket_id: str, user: AuthUser = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    from app.db.models import Ticket

    ticket = await db.scalar(select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == user.tenant_id))
    if not ticket:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工单不存在")
    return {
        "id": ticket.id,
        "title": ticket.title,
        "description": ticket.description,
        "severity": ticket.severity,
        "status": ticket.status,
        "approver": ticket.approver,
        "decision_comment": ticket.decision_comment,
        "created_at": ticket.created_at.isoformat(),
    }


@router.post("/api/admin/faults")
async def create_fault(body: FaultIn, user: AuthUser = Depends(require_roles("admin", "engineer")), db: AsyncSession = Depends(get_async_db)):
    record = FaultRecord(
        tenant_id=user.tenant_id,
        service=body.service.lower(),
        error_code=body.error_code.upper(),
        title=body.title,
        symptom=body.symptom,
        root_cause=body.root_cause,
        resolution=body.resolution,
        severity=body.severity,
    )
    db.add(record)
    await db.commit()
    return {"id": record.id, "service": record.service, "title": record.title}


@router.get("/api/admin/faults")
async def list_faults(
    service: str = Query(default=""),
    error_code: str = Query(default=""),
    user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    from app.agent.tools import query_fault_history

    records = await db.run_sync(query_fault_history, user.tenant_id, service=service, error_code=error_code, limit=50)
    return records


@router.get("/api/metrics/{service}")
async def get_metrics(service: str, window: int = Query(default=60, ge=5, le=240), user: AuthUser = Depends(get_current_user)):
    """实时指标查询（模拟 Prometheus）。"""
    return await run_in_threadpool(query_service_metrics, service, window_minutes=window, tenant_id=user.tenant_id)
