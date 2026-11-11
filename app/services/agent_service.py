"""Agent 服务层：运行诊断图、SSE 事件映射、审批恢复。"""
import json
import logging
import threading
from datetime import datetime, timezone

from fastapi import HTTPException, status
from langgraph.types import Command
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.agent.tools import TOOL_REGISTRY
from app.db.models import ApprovalRequest, Ticket
from app.services import chat_service, ticket_service
from app.services.budget import BudgetExceededError
from app.services.run_service import RunRecorder, capture_run, current_run, live_events
from app.services.support_service import context_for_session, redact

logger = logging.getLogger(__name__)
# shortcut: 单 API 进程串行审批；多实例部署时升级为跨进程协调。
_approval_lock = threading.Lock()


def _sse(data: dict) -> str:
    recorder = current_run()
    if recorder:
        data = {**data, "run_id": recorder.id}
        if data.get("type") in ("node_status", "tool_start", "approval_required", "error"):
            recorder.add("progress", data)
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _iter_graph_events(chunk_iter):
    """把 graph.stream(updates) 增量映射为事件流：节点更新到达即产出，不做整图缓存。"""
    delivered_tools = set()
    for chunk in chunk_iter:
        if isinstance(chunk, tuple):
            mode, chunk = chunk
            if mode == "custom":
                if chunk.get("type") == "tool":
                    delivered_tools.add(chunk.get("tool_call_id"))
                yield chunk
                continue
        for node, delta in chunk.items():
            if node == "__interrupt__":
                payloads = delta if isinstance(delta, (list, tuple)) else [delta]
                for intr in payloads:
                    value = getattr(intr, "value", None)
                    if value is None and isinstance(intr, dict):
                        value = intr
                    if value:
                        yield {"type": "approval_required", **value}
                continue
            if delta is None:
                continue
            yield {"type": "node_status", "node": node, "status": "completed"}
            for event in delta.get("events", []) or []:
                if event.get("type") == "tool" and event.get("tool_call_id") in delivered_tools:
                    continue
                yield event


def _emit_graph_events(graph, chunk_iter, events: list[dict]) -> None:
    """非流式路径：一次性收集全部事件（供返回值与测试使用）。"""
    events.extend(_iter_graph_events(chunk_iter))


def _final_snapshot(container, config: dict) -> dict:
    snapshot = container.graph.get_state(config)
    values = snapshot.values or {}
    return {
        "status": "approval_required" if snapshot.next else "answered" if values.get("end_reason", "answered") == "answered" else "failed",
        "answer": values.get("answer", ""),
        "generation_policy": values.get("generation_policy"),
        "citations": values.get("citations", []),
        "citations_indexed": values.get("citations_indexed", []),
        "hits": values.get("hits", []),
        "used_evidence": values.get("used_evidence"),
        "range_assembly": values.get("range_assembly"),
        "range_coverage": values.get("range_coverage"),
        "range_stats": values.get("range_stats"),
        "strategy": values.get("strategy", "hybrid"),
        "intent": values.get("intent", "diagnose"),
        "ticket_id": values.get("ticket_id"),
        "docs_sufficient": values.get("docs_sufficient"),
        "run_id": values.get("run_id"),
        "end_reason": values.get("end_reason", "answered"),
        "support_report": values.get("support_report"),
    }


def chat_once(container, db: Session, tenant_id: str, user_id: str, username: str, session_id: str, question: str) -> dict:
    if context_for_session(db, session_id, tenant_id, user_id) is not None:
        question = redact(question)["text"]
    recorder = RunRecorder.start(container, tenant_id, session_id, question)
    try:
        with capture_run(recorder):
            result = _chat_once(container, db, tenant_id, user_id, username, session_id, question, recorder.id)
        result["run_id"] = recorder.id
        recorder.finish(result)
        return result
    except BudgetExceededError as exc:
        # 预算门控拒绝（请求未到达供应商）：给出明确语义而非 5xx，便于操作者分辨额度与故障
        recorder.finish({"status": "budget_refused", "error": str(exc)[:200]})
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"预算不足：{exc}") from exc
    except Exception as exc:
        recorder.finish({"status": "failed", "error_type": type(exc).__name__})
        raise


def _chat_once(container, db, tenant_id, user_id, username, session_id, question, run_id):
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
        "run_id": run_id,
        "support_context": context_for_session(db, session_id, tenant_id, user_id),
        "support_report": None,
    }
    events: list[dict] = []
    _emit_graph_events(container.graph, container.graph.stream(input_state, config, stream_mode="updates"), events)
    # 图节点独立提交审批单；结束历史读取事务，避免 MySQL RR 快照看不到新审批。
    db.rollback()
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
        meta={"citations": result["citations"], "intent": result["intent"], "ticket_id": result["ticket_id"], "run_id": run_id},
    )
    return result


def chat_stream(container, tenant_id: str, user_id: str, username: str, session_id: str, question: str):
    with container.db_session() as db:
        if context_for_session(db, session_id, tenant_id, user_id) is not None:
            question = redact(question)["text"]
    def recorded():
        recorder = RunRecorder.start(container, tenant_id, session_id, question)
        iterator = _chat_stream(container, tenant_id, user_id, username, session_id, question, recorder.id)
        try:
            while True:
                # SSE每次next可由不同工作线程执行，ContextVar不能跨yield保留token。
                with capture_run(recorder), live_events():
                    item = next(iterator)
                yield item
        except StopIteration:
            return
        except GeneratorExit:
            if not recorder.finished:
                recorder.finish({"status": "cancelled", "end_reason": "client_disconnected"})
            raise
        except Exception as exc:
            recorder.finish({"status": "failed", "error_type": type(exc).__name__})
            yield _sse({"type": "error", "run_id": recorder.id, "error_type": type(exc).__name__,
                        "message": "调查执行失败，请保留运行编号并检查服务状态"})
        finally:
            with capture_run(recorder):
                iterator.close()

    return recorded()


def _chat_stream(container, tenant_id, user_id, username, session_id, question, run_id):
    """SSE 流式诊断：推送 start → 节点状态/检索/工具/回答 → (approval_required) → final → done。

    注意：不复用请求级 DB 会话，生成器内部自管会话，避免依赖在流式响应期间被提前关闭。
    """

    def generator():
        with container.db_session() as db:
            chat_service.add_message(db, tenant_id, session_id, "user", question)
            history = chat_service.get_history(db, session_id, limit=6)
            support_context = context_for_session(db, session_id, tenant_id, user_id)
        config = {"configurable": {"thread_id": session_id}}
        input_state = {
            "question": question,
            "session_id": session_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "username": username,
            "history": history,
            "run_id": run_id,
            "support_context": support_context,
            "support_report": None,
        }
        yield _sse({"type": "start", "session_id": session_id, "question": question})
        stream = container.graph.stream(input_state, config, stream_mode=["updates", "custom"])
        approval_emitted = False
        try:
            for event in _iter_graph_events(stream):
                if event.get("type") == "approval_required":
                    approval_emitted = True
                yield _sse(event)  # 到达即推送，避免先耗尽整图再补发
        except BudgetExceededError as exc:
            # 预算门控拒绝（请求未到达供应商）：流内给出明确错误事件，不让前端只见断流
            current_run().finish({"status": "budget_refused", "error": str(exc)[:200]})
            yield _sse({"type": "error", "error_type": "budget_exceeded", "message": str(exc)[:300],
                        "hint": "预算不足，请求在到达供应商前被拒绝（fail closed）；补充授权后重试即可恢复"})
            return
        with container.db_session() as db:
            events: list[dict] = []
            if not approval_emitted:
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
                meta={"citations": result["citations"], "intent": result["intent"], "ticket_id": result["ticket_id"], "run_id": run_id},
            )
        result["run_id"] = run_id
        current_run().finish(result)
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
    """先原子确认审批决定，再恢复图；重试使用既有决定与唯一工单。"""
    with _approval_lock:
        # 身份查询可能已开启 MySQL 一致性读事务，锁内重新开始以免读到旧审批状态。
        db.rollback()
        req = db.get(ApprovalRequest, approval_id)
        if req is None or req.tenant_id != auth.tenant_id:
            raise HTTPException(status_code=404, detail="审批单不存在")
        config = {"configurable": {"thread_id": req.session_id}}
        snapshot = container.graph.get_state(config)
        values = snapshot.values or {}
        if values.get("tenant_id") != req.tenant_id or values.get("approval_id") != req.id:
            raise HTTPException(status_code=409, detail="找不到该审批对应的持久化流程状态")
        target = "approved" if approved else "rejected"
        if req.status not in ("pending", target):
            raise HTTPException(status_code=409, detail="审批决定已确定，不能改为相反决定")
        if req.status == "pending":
            if not snapshot.next:
                raise HTTPException(status_code=409, detail="该流程没有等待审批的节点")
            changed = db.execute(
                update(ApprovalRequest)
                .where(ApprovalRequest.id == approval_id, ApprovalRequest.tenant_id == auth.tenant_id, ApprovalRequest.status == "pending")
                .values(status=target, comment=comment, decided_by=auth.username, decided_at=datetime.now(timezone.utc))
            ).rowcount
            db.commit()
            db.refresh(req)
            if changed != 1 and req.status != target:
                raise HTTPException(status_code=409, detail="审批已由其他请求处理")
        resume_value = {"approved": req.status == "approved", "comment": req.comment, "decided_by": req.decided_by}
        events: list[dict] = []
        if snapshot.next:
            interrupted = any(task.interrupts for task in snapshot.tasks)
            graph_input = Command(resume=resume_value) if interrupted else None
            _emit_graph_events(container.graph, container.graph.stream(graph_input, config, stream_mode="updates"), events)
            snapshot = container.graph.get_state(config)
            # prepare_approval 已提交、但进程在进入 interrupt 前崩溃时，先跑到等待点再恢复。
            if snapshot.next == ("approval_gate",) and any(task.interrupts for task in snapshot.tasks):
                _emit_graph_events(container.graph, container.graph.stream(Command(resume=resume_value), config, stream_mode="updates"), events)
            values = container.graph.get_state(config).values or {}
        ticket = db.query(Ticket).filter(Ticket.approval_id == approval_id, Ticket.tenant_id == auth.tenant_id).first()
        result = {
            "approval_id": approval_id,
            "approval_status": req.status,
            "ticket_id": ticket.id if ticket else values.get("ticket_id"),
            "answer": values.get("answer", ""),
            "events": events,
        }
        if values.get("run_id"):
            result["run_id"] = values["run_id"]
            recorder = RunRecorder(values["run_id"], auth.tenant_id)
            recorder.add("approval_decision", {"approved": approved, "decided_by": auth.username, "ticket_id": result["ticket_id"]})
            recorder.finish(result)
        return result


def list_tools() -> list[dict]:
    return TOOL_REGISTRY
