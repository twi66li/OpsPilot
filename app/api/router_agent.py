"""Agent 接口：SSE 流式诊断 / 非流式诊断 / 工单审批 / 工具清单。"""
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agent.tools import TOOL_REGISTRY
from app.api.schemas import ChatIn, DecisionIn
from app.auth.deps import AuthUser, get_current_user, require_roles
from app.container import get_container
from app.db.base import get_db
from app.services import agent_service, chat_service
from app.services.cache import check_rate_limit

router = APIRouter(prefix="/api/agent", tags=["agent"])


def _check_session_and_ratelimit(user: AuthUser, db: Session, session_id: str, question: str):
    container = get_container()
    session = chat_service.get_session(db, user.tenant_id, session_id)
    if not session:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    if not check_rate_limit(container.cache, f"{user.tenant_id}:{user.user_id}", container.settings.rate_limit_per_minute):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")
    return container


@router.post("/chat")
def chat(body: ChatIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """非流式诊断：一次返回最终结果与全部过程事件（便于测试与调试）。"""
    container = _check_session_and_ratelimit(user, db, body.session_id, body.message)
    return agent_service.chat_once(container, db, user.tenant_id, user.user_id, user.username, body.session_id, body.message)


@router.post("/chat/stream")
def chat_stream(body: ChatIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """SSE 流式诊断：推送节点状态、检索命中、工具事件、回答与审批请求。"""
    container = _check_session_and_ratelimit(user, db, body.session_id, body.message)
    generator = agent_service.chat_stream(container, user.tenant_id, user.user_id, user.username, body.session_id, body.message)
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions/{session_id}/pending-approval")
def pending_approval(session_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services import ticket_service

    approval = ticket_service.get_pending_approval(db, user.tenant_id, session_id)
    if not approval:
        return {"pending": False}
    return {"pending": True, "approval_id": approval.id, "ticket_draft": approval.ticket_draft}


@router.post("/approvals/{approval_id}/decide")
def decide(approval_id: str, body: DecisionIn, user: AuthUser = Depends(require_roles("admin", "engineer")), db: Session = Depends(get_db)):
    """人工审批工单创建（viewer 无权）：恢复 LangGraph 中断的流程。"""
    container = get_container()
    return agent_service.decide_approval(container, db, user, approval_id, body.approved, body.comment)


@router.get("/tools")
def tools(user: AuthUser = Depends(get_current_user)):
    return TOOL_REGISTRY


@router.get("/runs/{run_id}")
def get_run(run_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services.run_service import get_run as read_run

    run = read_run(db, user.tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return {"run_id": run.id, "session_id": run.session_id, "mode": run.mode, "status": run.status, "data": run.data}
