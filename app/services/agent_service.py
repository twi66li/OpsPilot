"""Agent 服务层：运行诊断图、SSE 事件映射、审批恢复。"""

import json
import logging
import threading
import time
from datetime import datetime, timezone

from fastapi import HTTPException, status
from langgraph.types import Command
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent.tools import TOOL_REGISTRY
from app.db.base import postgres_lock, run_with_sqlite_retry
from app.db.models import AgentRun, ApprovalRequest, Message, Ticket, _now
from app.services import chat_service, ticket_service
from app.services.budget import BudgetExceededError
from app.services.run_service import (
    LeaseLostError,
    RunCancelled,
    RunRecorder,
    capture_run,
    check_cancelled,
    check_lease,
    current_run,
    live_events,
    lock_run_row,
    make_message_idempotency_key,
)
from app.services.support_service import context_for_session, redact

logger = logging.getLogger(__name__)
# SQLite 离线环境使用进程锁；PostgreSQL 跨副本按审批单加锁。
_approval_lock = threading.Lock()
_ACTIVE_RUN_STATUSES = ("running", "cancel_requested")


def _assert_recorder_lease_on_row(db: Session, locked: AgentRun | None, recorder) -> None:
    if locked is None:
        raise LookupError("AgentRun 不存在")
    if recorder is None or not getattr(recorder, "has_lease", False):
        return
    if recorder.lease_lost.is_set() or (
        locked.lease_expires_at is None
        or locked.lease_expires_at <= _now()
        or locked.lease_owner != recorder.lease_owner
        or locked.lease_token != recorder.lease_token
        or locked.status not in _ACTIVE_RUN_STATUSES
    ):
        db.rollback()
        recorder.lease_lost.set()
        raise LeaseLostError(
            f"AgentRun 租约已失效: run_id={locked.id} expected=({recorder.lease_owner}, {recorder.lease_token}) "
            f"actual=({locked.lease_owner}, {locked.lease_token}, status={locked.status})"
        )


def _find_existing_run_message(db: Session, tenant_id: str, session_id: str, role: str, run_id: str | None):
    if not run_id:
        return None
    idem_key = make_message_idempotency_key(run_id, role)
    return (
        db.query(Message)
        .filter(
            Message.tenant_id == tenant_id,
            Message.session_id == session_id,
            Message.role == role,
            or_(
                Message.idempotency_key == idem_key,
                Message.meta["run_id"].as_string() == run_id,
            ),
        )
        .first()
    )


def _ensure_idempotent_message(
    db: Session,
    tenant_id: str,
    session_id: str,
    role: str,
    content: str,
    meta: dict | None = None,
    run_id: str | None = None,
) -> tuple[Message, bool]:
    """幂等写入带幂等键的会话消息，返回 (message, created)。调用方负责事务提交。"""
    idem_key = make_message_idempotency_key(run_id, role)
    existing = _find_existing_run_message(db, tenant_id, session_id, role, run_id)
    if existing is not None:
        return existing, False
    msg = Message(
        tenant_id=tenant_id,
        session_id=session_id,
        role=role,
        content=content,
        meta=meta or {},
        idempotency_key=idem_key,
    )
    try:
        nested = db.begin_nested() if hasattr(db, "begin_nested") else None
        if nested is not None and hasattr(nested, "__enter__"):
            with nested:
                db.add(msg)
                db.flush()
        else:
            db.add(msg)
            db.flush()
        return msg, True
    except IntegrityError:
        existing = _find_existing_run_message(db, tenant_id, session_id, role, run_id)
        if existing is not None:
            return existing, False
        raise


def _record_event(data: dict) -> dict:
    recorder = current_run()
    if recorder:
        data = {**data, "run_id": recorder.id}
        if data.get("type") in ("node_status", "tool_start", "approval_required", "error"):
            recorder.add("progress", data)
    return data


def _sse(data: dict) -> str:
    return f"data: {json.dumps(_record_event(data), ensure_ascii=False)}\n\n"


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
        "status": "approval_required"
        if snapshot.next
        else "answered"
        if values.get("end_reason", "answered") == "answered"
        else "failed",
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


def chat_once(
    container,
    db: Session,
    tenant_id: str,
    user_id: str,
    username: str,
    session_id: str,
    question: str,
    workflow="auto",
    request_id=None,
) -> dict:
    if context_for_session(db, session_id, tenant_id, user_id) is not None:
        question = redact(question)["text"]
    recorder = RunRecorder.start(container, tenant_id, session_id, question, request_id, workflow)
    try:
        with capture_run(recorder):
            result = _chat_once(
                container, db, tenant_id, user_id, username, session_id, question, recorder.id, workflow
            )
        result["run_id"] = recorder.id
        recorder.finish(result)
        return result
    except RunCancelled:
        recorder.finish({"status": "cancelled", "end_reason": "user_cancelled"})
        raise HTTPException(409, "运行已停止，已有证据保留")
    except BudgetExceededError as exc:
        # 预算门控拒绝（请求未到达供应商）：给出明确语义而非 5xx，便于操作者分辨额度与故障
        recorder.finish({"status": "budget_refused", "error": str(exc)[:200]})
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"预算不足：{exc}") from exc
    except Exception as exc:
        recorder.finish({"status": "failed", "error_type": type(exc).__name__})
        raise


def _chat_once(container, db, tenant_id, user_id, username, session_id, question, run_id, workflow="auto"):
    """非流式诊断：返回最终结果与全部过程事件（便于测试与调试）。"""
    _ensure_idempotent_message(db, tenant_id, session_id, "user", question, meta={"run_id": run_id}, run_id=run_id)
    db.commit()
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
        "evaluation_time": None,
        "support_context": context_for_session(db, session_id, tenant_id, user_id),
        "support_report": None,
        "support_workflow": workflow,
    }
    events: list[dict] = []
    _emit_graph_events(container.graph, container.graph.stream(input_state, config, stream_mode="updates"), events)
    # 图节点独立提交审批单，结束此前读取事务后再查询。
    db.rollback()
    result = _final_snapshot(container, config)
    _ensure_approval_event(container, db, config, tenant_id, session_id, events)
    result["session_id"] = session_id
    result["events"] = events
    approval = _pending_approval_payload(db, tenant_id, session_id)
    if approval:
        result["approval"] = approval
    check_cancelled()
    locked = lock_run_row(db, run_id, tenant_id)
    if locked is not None and locked.status == "cancel_requested":
        db.rollback()
        raise RunCancelled("用户已停止本次运行")
    _ensure_idempotent_message(
        db,
        tenant_id,
        session_id,
        "assistant",
        result["answer"],
        meta={
            "citations": result["citations"],
            "intent": result["intent"],
            "ticket_id": result["ticket_id"],
            "run_id": run_id,
        },
        run_id=run_id,
    )
    db.commit()
    return result


def chat_stream(
    container,
    tenant_id: str,
    user_id: str,
    username: str,
    session_id: str,
    question: str,
    workflow="auto",
    request_id=None,
):
    with container.db_session() as db:
        if context_for_session(db, session_id, tenant_id, user_id) is not None:
            question = redact(question)["text"]
    recorder = RunRecorder.start(container, tenant_id, session_id, question, request_id, workflow)

    def recorded():
        iterator = _chat_stream(container, tenant_id, user_id, username, session_id, question, recorder.id, workflow)
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
                recorder.cancel_signal.set()
                recorder.finish({"status": "cancelled", "end_reason": "client_disconnected"})
            raise
        except RunCancelled:
            recorder.finish({"status": "cancelled", "end_reason": "user_cancelled"})
            yield _sse({"type": "cancelled", "message": "已停止，已有证据保留"})
            yield _sse({"type": "done"})
        except Exception as exc:
            recorder.finish({"status": "failed", "error_type": type(exc).__name__})
            yield _sse(
                {
                    "type": "error",
                    "run_id": recorder.id,
                    "error_type": type(exc).__name__,
                    "message": "调查执行失败，请保留运行编号并检查服务状态",
                }
            )
        finally:
            with capture_run(recorder):
                iterator.close()

    return recorded()


def _background_input(db, run, tenant_id, session_id, input_state):
    run_id = run.id
    recorder = current_run()
    lease_recorder = recorder if (recorder is not None and recorder.id == run_id and recorder.has_lease) else None

    def _apply():
        try:
            locked = lock_run_row(db, run_id, tenant_id)
            if locked is None:
                raise LookupError(f"AgentRun 不存在: {run_id}")
            _assert_recorder_lease_on_row(db, locked, lease_recorder)
            saved = locked.data.get("background_input")
            if saved:
                return saved
            prior = chat_service.get_history(db, session_id, limit=5)
            prepared = {**input_state, "history": prior + [{"role": "user", "content": input_state["question"]}]}
            _ensure_idempotent_message(
                db,
                tenant_id,
                session_id,
                "user",
                prepared["question"],
                meta={"run_id": locked.id},
                run_id=locked.id,
            )
            new_data = {**locked.data, "background_input": prepared}
            if lease_recorder is not None:
                updated = db.execute(
                    update(AgentRun)
                    .where(
                        AgentRun.id == run_id,
                        AgentRun.tenant_id == tenant_id,
                        AgentRun.lease_owner == lease_recorder.lease_owner,
                        AgentRun.lease_token == lease_recorder.lease_token,
                        AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
                    )
                    .values(data=new_data, updated_at=_now())
                ).rowcount
                if updated != 1:
                    db.rollback()
                    lease_recorder.lease_lost.set()
                    raise LeaseLostError(f"AgentRun 租约已失效，拒绝写入背景输入: run_id={run_id}")
            else:
                locked.data = new_data
            db.commit()
            return prepared
        except Exception:
            db.rollback()
            raise

    return run_with_sqlite_retry(_apply)


def _chat_stream(container, tenant_id, user_id, username, session_id, question, run_id, workflow="auto", *, background=False):
    """SSE 流式诊断：推送 start → 节点状态/检索/工具/回答 → (approval_required) → final → done。

    注意：不复用请求级 DB 会话，生成器内部自管会话，避免依赖在流式响应期间被提前关闭。
    """

    def generator():
        event_data = _record_event if background else _sse
        config = {"configurable": {"thread_id": session_id}}
        base_input = {
            "question": question,
            "session_id": session_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "username": username,
            "run_id": run_id,
            "evaluation_time": None,
            "support_report": None,
            "support_workflow": workflow,
        }

        def _prepare_input():
            with container.db_session() as db:
                if background:
                    run = db.get(AgentRun, run_id)
                    saved_input = run.data.get("background_input") if run else None
                    if saved_input:
                        return saved_input
                    prepared = {
                        **base_input,
                        "support_context": context_for_session(db, session_id, tenant_id, user_id),
                        "evaluation_time": round(time.time(), 3),
                    }
                    return _background_input(db, run, tenant_id, session_id, prepared)
                _ensure_idempotent_message(
                    db, tenant_id, session_id, "user", question, meta={"run_id": run_id}, run_id=run_id
                )
                db.commit()
                return {
                    **base_input,
                    "history": chat_service.get_history(db, session_id, limit=6),
                    "support_context": context_for_session(db, session_id, tenant_id, user_id),
                }

        input_state = run_with_sqlite_retry(_prepare_input)
        yield event_data({"type": "start", "session_id": session_id, "question": question})
        snapshot = container.graph.get_state(config) if background else None
        resumed = snapshot is not None and snapshot.values.get("run_id") == run_id
        if resumed:
            yield event_data({"type": "resumed", "message": "已从保存的检查点继续"})
            yield event_data({"type": "answer_reset"})
        if resumed and (not snapshot.next or any(task.interrupts for task in snapshot.tasks)):
            stream = iter(())
        else:
            stream = container.graph.stream(
                None if resumed else input_state, config, stream_mode=["updates", "custom"],
                **({"durability": "sync"} if background else {}),
            )
        approval_emitted = False
        try:
            for event in _iter_graph_events(stream):
                check_cancelled()
                if event.get("type") == "approval_required":
                    approval_emitted = True
                yield event_data(event)
        except BudgetExceededError as exc:
            # 预算门控拒绝（请求未到达供应商）：流内给出明确错误事件，不让前端只见断流
            current_run().finish({"status": "budget_refused", "error": str(exc)[:200]})
            yield event_data(
                {
                    "type": "error",
                    "error_type": "budget_exceeded",
                    "message": str(exc)[:300],
                    "hint": "预算不足，请求在到达供应商前被拒绝（fail closed）；补充授权后重试即可恢复",
                }
            )
            return
        with container.db_session() as db:
            events: list[dict] = []
            if not approval_emitted:
                _ensure_approval_event(container, db, config, tenant_id, session_id, events)
        for event in events:
            yield event_data(event)
        check_cancelled()
        result = _final_snapshot(container, config)

        def _persist_assistant():
            recorder = current_run()
            lease_recorder = (
                recorder if (background and recorder is not None and recorder.id == run_id and recorder.has_lease) else None
            )
            with container.db_session() as db:
                locked = lock_run_row(db, run_id, tenant_id)
                if background:
                    _assert_recorder_lease_on_row(db, locked, lease_recorder)
                if locked is not None and locked.status == "cancel_requested":
                    raise RunCancelled("用户已停止本次运行")
                approval = _pending_approval_payload(db, tenant_id, session_id)
                _ensure_idempotent_message(
                    db,
                    tenant_id,
                    session_id,
                    "assistant",
                    result["answer"],
                    meta={
                        "citations": result["citations"],
                        "intent": result["intent"],
                        "ticket_id": result["ticket_id"],
                        "run_id": run_id,
                    },
                    run_id=run_id,
                )
                if lease_recorder is not None:
                    now = _now()
                    updated = db.execute(
                        update(AgentRun)
                        .where(
                            AgentRun.id == run_id,
                            AgentRun.tenant_id == tenant_id,
                            AgentRun.lease_owner == lease_recorder.lease_owner,
                            AgentRun.lease_token == lease_recorder.lease_token,
                            AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
                        )
                        .values(heartbeat_at=now, updated_at=now)
                    ).rowcount
                    if updated != 1:
                        db.rollback()
                        lease_recorder.lease_lost.set()
                        raise LeaseLostError(f"AgentRun 租约已失效，拒绝写入助手消息: run_id={run_id}")
                db.commit()
                return approval

        check_lease()
        approval = run_with_sqlite_retry(_persist_assistant)
        if approval:
            result["approval"] = approval
        result["run_id"] = run_id
        current_run().finish(result)
        yield event_data({**result, "type": "final"})
        yield event_data({"type": "done"})

    return generator()


def _ensure_approval_event(
    container, db: Session, config: dict, tenant_id: str, session_id: str, events: list[dict]
) -> None:
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
    # 等待审批锁之前释放请求事务占用的连接。
    db.rollback()
    bind = db.get_bind()
    multi = getattr(container.settings, "multi_replica_enabled", False) and bind.dialect.name == "postgresql"
    lock = postgres_lock(f"opspilot:approval:{approval_id}", blocking=False) if multi else _approval_lock
    with lock as acquired:
        if multi and not acquired:
            raise HTTPException(status_code=409, detail="另一个副本正在处理该审批，请稍后重试")
        req = db.get(ApprovalRequest, approval_id)
        if req is None or req.tenant_id != auth.tenant_id:
            raise HTTPException(status_code=404, detail="审批单不存在")
        if req.status == "cancelled":
            raise HTTPException(status_code=409, detail="会话已删除，待审批动作已取消")
        config = {"configurable": {"thread_id": req.session_id}}
        snapshot = container.graph.get_state(config)
        values = snapshot.values or {}
        if values.get("tenant_id") != req.tenant_id or values.get("approval_id") != req.id:
            raise HTTPException(status_code=409, detail="找不到该审批对应的持久化流程状态")
        target = "approved" if approved else "rejected"
        if req.status not in ("pending", target):
            raise HTTPException(status_code=409, detail="审批决定已确定，不能改为相反决定")
        from app.services.preflight import evaluate_ticket_preflight

        preflight = evaluate_ticket_preflight(
            req.ticket_draft,
            requested_by=req.requested_by,
            approver_username=auth.username,
            approver_role=auth.role,
            strict_four_eyes=getattr(container.settings, "strict_four_eyes", False),
        )
        if req.status == "pending":
            if approved and not preflight["passed"]:
                raise HTTPException(status_code=403, detail=preflight["summary"])
            if not snapshot.next:
                raise HTTPException(status_code=409, detail="该流程没有等待审批的节点")
            changed = db.execute(
                update(ApprovalRequest)
                .where(
                    ApprovalRequest.id == approval_id,
                    ApprovalRequest.tenant_id == auth.tenant_id,
                    ApprovalRequest.status == "pending",
                )
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
            _emit_graph_events(
                container.graph, container.graph.stream(graph_input, config, stream_mode="updates"), events
            )
            snapshot = container.graph.get_state(config)
            # prepare_approval 已提交、但进程在进入 interrupt 前崩溃时，先跑到等待点再恢复。
            if snapshot.next == ("approval_gate",) and any(task.interrupts for task in snapshot.tasks):
                _emit_graph_events(
                    container.graph,
                    container.graph.stream(Command(resume=resume_value), config, stream_mode="updates"),
                    events,
                )
            values = container.graph.get_state(config).values or {}
        ticket = db.query(Ticket).filter(Ticket.approval_id == approval_id, Ticket.tenant_id == auth.tenant_id).first()
        result = {
            "approval_id": approval_id,
            "approval_status": req.status,
            "ticket_id": ticket.id if ticket else values.get("ticket_id"),
            "answer": values.get("answer", ""),
            "preflight": preflight,
            "events": events,
        }
        if values.get("run_id"):
            result["run_id"] = values["run_id"]
            recorder = RunRecorder(values["run_id"], auth.tenant_id)
            recorder.add(
                "approval_decision",
                {"approved": approved, "decided_by": auth.username, "ticket_id": result["ticket_id"]},
            )
            recorder.finish(result)
        return result


def list_tools() -> list[dict]:
    return TOOL_REGISTRY
