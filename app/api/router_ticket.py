"""工单 / 故障记录 / 服务指标接口。"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.api.schemas import FaultIn
from app.auth.deps import AuthUser, get_current_user, require_roles
from app.db.base import SessionLocal
from app.db.models import FaultRecord
from app.services import ticket_service
from app.services.metrics_service import query_service_metrics

router = APIRouter(tags=["ops"])


@router.get("/api/tickets")
def list_tickets(user: AuthUser = Depends(require_roles("admin", "engineer"))):
    with SessionLocal() as db:
        tickets = ticket_service.list_tickets(db, user.tenant_id)
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
def get_ticket(ticket_id: str, user: AuthUser = Depends(require_roles("admin", "engineer"))):
    from app.db.models import Ticket

    with SessionLocal() as db:
        ticket = db.scalar(select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == user.tenant_id))
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
def create_fault(body: FaultIn, user: AuthUser = Depends(require_roles("admin", "engineer"))):
    with SessionLocal() as db:
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
        db.commit()
        return {"id": record.id, "service": record.service, "title": record.title}


@router.get("/api/admin/faults")
def list_faults(
    service: str = Query(default=""),
    error_code: str = Query(default=""),
    user: AuthUser = Depends(get_current_user),
):
    from app.agent.tools import query_fault_history

    with SessionLocal() as db:
        return query_fault_history(db, user.tenant_id, service=service, error_code=error_code, limit=50)


@router.get("/api/metrics/{service}")
async def get_metrics(service: str, window: int = Query(default=60, ge=5, le=240), user: AuthUser = Depends(get_current_user)):
    """实时指标查询（模拟 Prometheus）。"""
    return await run_in_threadpool(query_service_metrics, service, window_minutes=window, tenant_id=user.tenant_id)
