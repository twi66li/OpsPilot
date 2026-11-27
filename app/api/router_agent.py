"""Agent 接口：SSE 流式诊断 / 非流式诊断 / 工单审批 / 工具清单。"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.agent.tools import DRILL_TOOLS, TOOL_REGISTRY
from app.api.schemas import ChatIn, DecisionIn
from app.auth.deps import AuthUser, get_assistant_user, get_current_user, require_roles
from app.container import get_container
from app.db.base import SessionLocal, get_db
from app.db.models import SupportCase
from app.services import agent_service, background_runs, chat_service
from app.services.assistant_quota import claim_chat, model_allowance
from app.services.cache import check_rate_limit
from app.services.case_service import assert_editable
from app.services.custom_model import custom_llm_context, parse_custom_model_headers
from app.services.visitor_service import DEMO_DRILL_PREFIX, VISITOR_ID

router = APIRouter(prefix="/api/agent", tags=["agent"])
_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


def _check_session_and_ratelimit(user: AuthUser, db: Session, session_id: str, workflow: str):
    container = get_container()
    session = chat_service.get_session(db, user.tenant_id, session_id, user.user_id)
    if not session:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    if session.assistant_chat != (workflow == "chat") or (user.is_guest and not session.assistant_chat):
        raise HTTPException(403, "请在对应模式中继续对话")
    personal = db.get(SupportCase, session_id)
    if personal:
        assert_editable(personal)
    if not check_rate_limit(
        container.cache, f"{user.tenant_id}:{user.user_id}", container.settings.rate_limit_per_minute
    ):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")
    return container


@router.post("/chat")
def chat(body: ChatIn, request: Request, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """非流式诊断：一次返回最终结果与全部过程事件（便于测试与调试）。"""
    container = _check_session_and_ratelimit(user, db, body.session_id, body.workflow)
    custom_model = parse_custom_model_headers(request, allow_loopback=container.settings.app_env != "production")
    existing = existing_request(db, body, user)
    if existing:
        return JSONResponse(
            {**existing.data.get("result", {}), "run_id": existing.id, "status": existing.status},
            status_code=202 if existing.status in ("running", "cancel_requested") else 200,
        )
    db.rollback()
    claim_chat(user, body.session_id)
    with model_allowance(user.user_id, user.tenant_id, user.role), custom_llm_context(custom_model):
        return agent_service.chat_once(
            container, db, user.tenant_id, user.user_id, user.username, body.session_id,
            body.message, body.workflow, body.request_id,
        )


@router.post("/chat/stream")
def chat_stream(body: ChatIn, request: Request, user: AuthUser = Depends(get_assistant_user)):
    """SSE 流式诊断：推送节点状态、检索命中、工具事件、回答与审批请求。"""
    # 前置校验使用短生命周期同步工作单元，流式响应开始前立即归还数据库连接。
    with SessionLocal() as db:
        container = _check_session_and_ratelimit(user, db, body.session_id, body.workflow)
        custom_model = parse_custom_model_headers(request, allow_loopback=container.settings.app_env != "production")
        existing = existing_request(db, body, user)
        existing_info = (
            {
                "id": existing.id,
                "status": existing.status,
                "background": bool(existing.data.get("background")),
                "result": dict(existing.data.get("result", {})),
            }
            if existing is not None
            else None
        )
    if existing_info is None:
        claim_chat(user, body.session_id)
    # 多副本下自带模型凭据不落盘，不跨进程入队；单副本时走进程内短暂绑定，仍支持断线续传与停止生成。
    use_background = body.background and not (custom_model is not None and container.settings.multi_replica_enabled)
    if use_background and (existing_info is None or existing_info["background"]):
        if existing_info is None:
            try:
                run_id = background_runs.enqueue(container, user, body, custom_model=custom_model)
            except HTTPException as exc:
                # 断线重试可能先于原请求提交返回。同一请求只订阅已经创建的运行。
                with SessionLocal() as db:
                    retry_existing = existing_request(db, body, user) if exc.status_code == 409 else None
                    if retry_existing is None or not retry_existing.data.get("background"):
                        raise
                    run_id = retry_existing.id
        else:
            run_id = existing_info["id"]
        generator = background_runs.subscribe(run_id, user.tenant_id, user.user_id)
    elif existing_info is not None:
        import json

        data = {
            **existing_info["result"],
            "type": "existing_run",
            "run_id": existing_info["id"],
            "status": existing_info["status"],
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
            role=user.role,
            custom_model=custom_model,
        )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/runs/{run_id}/events")
def run_events(
    run_id: str,
    after: int = Query(default=0, ge=0),
    user: AuthUser = Depends(get_assistant_user),
):
    from app.services.run_service import get_run

    with SessionLocal() as db:
        run = get_run(db, user.tenant_id, run_id, user.user_id)
        if run is None or not run.data.get("background"):
            raise HTTPException(404, "找不到可订阅的运行")
    return StreamingResponse(
        background_runs.subscribe(run_id, user.tenant_id, user.user_id, after),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
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
    from app.services.preflight import evaluate_ticket_preflight

    strict = get_container().settings.strict_four_eyes
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
            "preflight": evaluate_ticket_preflight(
                item.ticket_draft,
                requested_by=item.requested_by,
                approver_username=user.username,
                approver_role=user.role,
                strict_four_eyes=strict,
            ),
        }
        for item in pending
    ]


@router.get("/skills")
def skills(user: AuthUser = Depends(get_current_user)):
    from app.agent.skills import list_skills

    return [item for item in list_skills() if item["name"] != "drill" or get_container().settings.drill_enabled]


@router.get("/tools")
def tools(user: AuthUser = Depends(get_current_user)):
    settings = get_container().settings
    registry = [tool for tool in TOOL_REGISTRY if settings.drill_enabled or tool["name"] not in DRILL_TOOLS]
    if settings.retrieval_subagent_enabled:
        from app.agent.tool_loop import DESCRIPTIONS

        return [tool for tool in registry if tool["name"] != "search_runbook"] + [
            {"name": "delegate_retrieval", "description": DESCRIPTIONS["delegate_retrieval"]}
        ]
    return registry


@router.get("/runs/{run_id}")
def get_run(run_id: str, user: AuthUser = Depends(get_assistant_user), db: Session = Depends(get_db)):
    from app.services.run_service import get_run as read_run

    owner = user.user_id
    if user.is_guest and run_id.startswith(DEMO_DRILL_PREFIX) and get_container().settings.drill_enabled:
        owner = VISITOR_ID
    run = read_run(db, user.tenant_id, run_id, owner)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    from app.services.run_service import serialize_run

    return serialize_run(db, run)


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, user: AuthUser = Depends(get_assistant_user), db: Session = Depends(get_db)):
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
