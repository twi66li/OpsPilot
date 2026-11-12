"""Agent 接口：SSE 流式诊断 / 非流式诊断 / 工单审批 / 工具清单。"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.agent.tools import TOOL_REGISTRY
from app.api.schemas import ChatIn, DecisionIn
from app.auth.deps import AuthUser, get_current_user, require_roles
from app.container import get_container
from app.db.base import get_db
from app.db.models import SupportCase
from app.services import agent_service, chat_service
from app.services.cache import check_rate_limit
from app.services.case_service import assert_editable

router = APIRouter(prefix="/api/agent", tags=["agent"])


def _check_session_and_ratelimit(user: AuthUser, db: Session, session_id: str, question: str):
    container = get_container()
    session = chat_service.get_session(db, user.tenant_id, session_id, user.user_id)
    if not session:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    personal = db.get(SupportCase, session_id)
    if personal:
        assert_editable(personal)
    if not check_rate_limit(
        container.cache, f"{user.tenant_id}:{user.user_id}", container.settings.rate_limit_per_minute
    ):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")
    return container


@router.post("/chat")
def chat(body: ChatIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """非流式诊断：一次返回最终结果与全部过程事件（便于测试与调试）。"""
    container = _check_session_and_ratelimit(user, db, body.session_id, body.message)
    existing = existing_request(db, body, user)
    if existing:
        return JSONResponse(
            {**existing.data.get("result", {}), "run_id": existing.id, "status": existing.status},
            status_code=202 if existing.status in ("running", "cancel_requested") else 200,
        )
    return agent_service.chat_once(
        container,
        db,
        user.tenant_id,
        user.user_id,
        user.username,
        body.session_id,
        body.message,
        body.workflow,
        body.request_id,
    )


@router.post("/chat/stream")
def chat_stream(body: ChatIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """SSE 流式诊断：推送节点状态、检索命中、工具事件、回答与审批请求。"""
    container = _check_session_and_ratelimit(user, db, body.session_id, body.message)
    existing = existing_request(db, body, user)
    if existing:
        import json

        data = {
            **existing.data.get("result", {}),
            "type": "existing_run",
            "run_id": existing.id,
            "status": existing.status,
        }
        generator = iter(["data: " + json.dumps(data, ensure_ascii=False) + "\n\n", 'data: {"type":"done"}\n\n'])
    else:
        generator = agent_service.chat_stream(
            container,
            user.tenant_id,
            user.user_id,
            user.username,
            body.session_id,
            body.message,
            body.workflow,
            body.request_id,
        )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions/{session_id}/pending-approval")
def pending_approval(session_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services import ticket_service

    if not chat_service.get_session(db, user.tenant_id, session_id, user.user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    approval = ticket_service.get_pending_approval(db, user.tenant_id, session_id)
    if not approval:
        return {"pending": False}
    return {"pending": True, "approval_id": approval.id, "ticket_draft": approval.ticket_draft}


@router.post("/approvals/{approval_id}/decide")
def decide(
    approval_id: str,
    body: DecisionIn,
    user: AuthUser = Depends(require_roles("admin", "engineer")),
    db: Session = Depends(get_db),
):
    """人工审批工单创建（viewer 无权）：恢复 LangGraph 中断的流程。"""
    container = get_container()
    return agent_service.decide_approval(container, db, user, approval_id, body.approved, body.comment)


@router.get("/approvals")
def list_approvals(user: AuthUser = Depends(require_roles("admin", "engineer")), db: Session = Depends(get_db)):
    from app.db.models import ApprovalRequest

    pending = (
        db.query(ApprovalRequest)
        .filter_by(tenant_id=user.tenant_id, status="pending")
        .order_by(ApprovalRequest.created_at.desc())
        .limit(100)
        .all()
    )
    return [
        {
            "approval_id": item.id,
            "session_id": item.session_id,
            "ticket_draft": item.ticket_draft,
            "requested_by": item.requested_by,
            "created_at": item.created_at.isoformat(),
        }
        for item in pending
    ]


@router.get("/tools")
def tools(user: AuthUser = Depends(get_current_user)):
    settings = get_container().settings
    if settings.retrieval_subagent_enabled:
        from app.agent.tool_loop import DESCRIPTIONS

        return [tool for tool in TOOL_REGISTRY if tool["name"] != "search_runbook"] + [
            {"name": "delegate_retrieval", "description": DESCRIPTIONS["delegate_retrieval"]}
        ]
    return TOOL_REGISTRY


@router.get("/runs/{run_id}")
def get_run(run_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services.run_service import get_run as read_run

    run = read_run(db, user.tenant_id, run_id, user.user_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    from app.services.run_service import serialize_run

    return serialize_run(db, run)


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services.run_service import get_run, request_cancel

    run = get_run(db, user.tenant_id, run_id, user.user_id)
    if run is None:
        raise HTTPException(404, "运行不存在")
    return {"run_id": run.id, "status": request_cancel(db, run)}


def existing_request(db, body, user):
    from app.db.models import AgentRun
    from app.services.support_service import redact

    if not body.request_id:
        return None
    run = (
        db.query(AgentRun)
        .filter_by(session_id=body.session_id, tenant_id=user.tenant_id, request_id=body.request_id)
        .first()
    )
    if run and (
        run.data["question"] not in (body.message, redact(body.message)["text"])
        or run.data.get("workflow", "auto") != body.workflow
    ):
        raise HTTPException(409, "同一请求编号不能用于不同问题或工作流")
    return run
