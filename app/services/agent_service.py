"""Agent 服务层：运行诊断图、SSE 事件映射、审批恢复。"""
import json
import logging

from fastapi import HTTPException
from langgraph.types import Command
from sqlalchemy.orm import Session

from app.agent.tools import TOOL_REGISTRY
from app.db.models import ApprovalRequest
from app.services import chat_service, ticket_service

logger = logging.getLogger(__name__)


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _emit_graph_events(graph, chunk_iter, events: list[dict]) -> None:
    for chunk in chunk_iter:
        for node, delta in chunk.items():
            if node == "__interrupt__":
                payloads = delta if isinstance(delta, list) else [delta]
                for intr in payloads:
                    value = getattr(intr, "value", None)
                    if value is None and isinstance(intr, dict):
                        value = intr
                    if value:
                        events.append({"type": "approval_required", **value})
                continue
            if delta is None:
                continue
            events.append({"type": "node_status", "node": node, "status": "completed"})
            for event in delta.get("events", []) or []:
                events.append(event)


def _final_snapshot(container, config: dict) -> dict:
    snapshot = container.graph.get_state(config)
    values = snapshot.values or {}
    return {
        "status": "approval_required" if snapshot.next else "answered",
        "answer": values.get("answer", ""),
        "citations": values.get("citations", []),
        "intent": values.get("intent", "diagnose"),
        "ticket_id": values.get("ticket_id"),
        "docs_sufficient": values.get("docs_sufficient"),
    }


def chat_once(container, db: Session, tenant_id: str, user_id: str, username: str, session_id: str, question: str) -> dict:
    """非流式诊断：返回最终结果与全部过程事件（便于测试与调试）。"""
    chat_service.add_message(db, tenant_id, session_id, "user", question)
    history = chat_service.get_history(db, session_id, limit=6)
    config = {"configurable": {"thread_id": session_id}}
    input_state = {
        "question": question,
        "session_id": session_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "username": username,
        "history": history,
    }
    events: list[dict] = []
    _emit_graph_events(container.graph, container.graph.stream(input_state, config, stream_mode="updates"), events)
    result = _final_snapshot(container, config)
    _ensure_approval_event(container, db, config, tenant_id, session_id, events)
    result["session_id"] = session_id
    result["events"] = events
    approval = _pending_approval_payload(db, tenant_id, session_id)
    if approval:
        result["approval"] = approval
    chat_service.add_message(
        db,
        tenant_id,
        session_id,
        "assistant",
        result["answer"],
        meta={"citations": result["citations"], "intent": result["intent"], "ticket_id": result["ticket_id"]},
    )
    return result


def chat_stream(container, tenant_id: str, user_id: str, username: str, session_id: str, question: str):
    """SSE 流式诊断：推送 start → 节点状态/检索/工具/回答 → (approval_required) → final → done。

    注意：不复用请求级 DB 会话，生成器内部自管会话，避免依赖在流式响应期间被提前关闭。
    """

    def generator():
        with container.db_session() as db:
            chat_service.add_message(db, tenant_id, session_id, "user", question)
            history = chat_service.get_history(db, session_id, limit=6)
        config = {"configurable": {"thread_id": session_id}}
        input_state = {
            "question": question,
            "session_id": session_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "username": username,
            "history": history,
        }
        yield _sse({"type": "start", "session_id": session_id, "question": question})
        events: list[dict] = []
        _emit_graph_events(container.graph, container.graph.stream(input_state, config, stream_mode="updates"), events)
        with container.db_session() as db:
            _ensure_approval_event(container, db, config, tenant_id, session_id, events)
        for event in events:
            yield _sse(event)
        result = _final_snapshot(container, config)
        with container.db_session() as db:
            approval = _pending_approval_payload(db, tenant_id, session_id)
            if approval:
                result["approval"] = approval
            chat_service.add_message(
                db,
                tenant_id,
                session_id,
                "assistant",
                result["answer"],
                meta={"citations": result["citations"], "intent": result["intent"], "ticket_id": result["ticket_id"]},
            )
        yield _sse({**result, "type": "final"})
        yield _sse({"type": "done"})

    return generator()


def _ensure_approval_event(container, db: Session, config: dict, tenant_id: str, session_id: str, events: list[dict]) -> None:
    """兜底：部分 langgraph 版本不把 interrupt 放进 updates 流，用挂起的审批单补齐事件。"""
    snapshot = container.graph.get_state(config)
    if snapshot.next and not any(e.get("type") == "approval_required" for e in events):
        approval = _pending_approval_payload(db, tenant_id, session_id)
        if approval:
            events.append({"type": "approval_required", **approval})


def _pending_approval_payload(db: Session, tenant_id: str, session_id: str) -> dict | None:
    req = ticket_service.get_pending_approval(db, tenant_id, session_id)
    if req is None:
        return None
    return {"approval_id": req.id, "ticket_draft": req.ticket_draft, "session_id": req.session_id}


def decide_approval(container, db: Session, auth, approval_id: str, approved: bool, comment: str = "") -> dict:
    """人工审批：恢复被 interrupt 的图，返回最终结果。"""
    req = db.get(ApprovalRequest, approval_id)
    if req is None or req.tenant_id != auth.tenant_id:
        raise HTTPException(status_code=404, detail="审批单不存在")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail=f"该审批单已处理（{req.status}）")
    config = {"configurable": {"thread_id": req.session_id}}
    snapshot = container.graph.get_state(config)
    if not snapshot.next:
        raise HTTPException(status_code=409, detail="该会话没有等待审批的诊断流程（可能服务已重启丢失内存态）")
    resume_value = {"approved": approved, "comment": comment, "decided_by": auth.username}
    events: list[dict] = []
    _emit_graph_events(container.graph, container.graph.stream(Command(resume=resume_value), config, stream_mode="updates"), events)
    values = container.graph.get_state(config).values or {}
    db.refresh(req)
    return {
        "approval_id": approval_id,
        "approval_status": req.status,
        "ticket_id": values.get("ticket_id"),
        "answer": values.get("answer", ""),
        "events": events,
    }


def list_tools() -> list[dict]:
    return TOOL_REGISTRY
