"""单 API 实例内独立消费持久化调查任务，SSE 只订阅已保存的事件。"""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from app.db.base import SessionLocal
from app.db.models import AgentRun, AgentRunEvent, ChatSession, SupportCase, User
from app.services import agent_service
from app.services.case_service import assert_editable
from app.services.run_service import RunCancelled, RunRecorder, capture_run, check_cancelled, get_run, live_events
from app.services.support_service import redact

logger = logging.getLogger(__name__)
_worker = None
ACTIVE = ("running", "cancel_requested")


def start_worker(container):
    global _worker
    with SessionLocal() as db:
        runs = db.query(AgentRun).filter(
            AgentRun.status.in_(ACTIVE), AgentRun.data["background"].as_boolean().is_(True)
        ).order_by(AgentRun.created_at).all()
        pending = []
        for run in runs:
            if run.status == "cancel_requested":
                run.status = "cancelled"
                run.data = {**run.data, "result": {"end_reason": "user_cancelled"}}
            else:
                pending.append(run.id)
        db.commit()
    _worker = ThreadPoolExecutor(max_workers=max(1, container.settings.max_concurrent_runs),
                                 thread_name_prefix="diagnosis")
    for run_id in pending:
        _worker.submit(execute, container, run_id)


def stop_worker():
    global _worker
    if _worker:
        _worker.shutdown(wait=True)
        _worker = None


def enqueue(container, user, body):
    if _worker is None:
        raise HTTPException(503, "调查后台尚未就绪")
    with SessionLocal() as db:
        personal = db.get(SupportCase, body.session_id)
    question = redact(body.message)["text"] if personal else body.message
    recorder = RunRecorder.start(container, user.tenant_id, body.session_id, question,
                                 body.request_id, body.workflow, background=True)
    _worker.submit(execute, container, recorder.id)
    return recorder.id


def execute(container, run_id):
    with SessionLocal() as db:
        run = db.get(AgentRun, run_id)
        if not run or run.status not in ACTIVE:
            return
        session = db.get(ChatSession, run.session_id)
        user = db.get(User, session.user_id) if session and session.tenant_id == run.tenant_id and session.deleted_at is None else None
        personal = db.get(SupportCase, run.session_id)
    recorder = RunRecorder(run.id, run.tenant_id)
    try:
        with capture_run(recorder), live_events():
            if run.status == "cancel_requested" or not user or user.tenant_id != run.tenant_id:
                raise RunCancelled()
            if personal:
                assert_editable(personal)
            stream = agent_service._chat_stream(
                container, run.tenant_id, user.id, user.username, run.session_id,
                run.data["question"], run.id, run.data["workflow"], background=True,
            )
            tokens = None
            flushed_at = time.monotonic()
            try:
                for event in stream:
                    check_cancelled()
                    if event["type"] == "token":
                        if tokens is None:
                            tokens = event
                        else:
                            tokens["delta"] += event["delta"]
                        if time.monotonic() - flushed_at < 0.25 and len(tokens["delta"]) < 1024:
                            continue
                    if tokens is not None:
                        recorder.add("stream", tokens)
                        tokens = None
                        flushed_at = time.monotonic()
                    # 终态和完整回答直接从 AgentRun 读取。
                    if event["type"] not in ("token", "error", "final", "done"):
                        recorder.add("stream", event)
            finally:
                stream.close()
    except RunCancelled:
        if not recorder.finished:
            recorder.finish({"status": "cancelled", "end_reason": "user_cancelled"})
    except Exception as exc:
        logger.error("background_run_failed run_id=%s error_type=%s", run_id, type(exc).__name__)
        if not recorder.finished:
            recorder.finish({"status": "failed", "error_type": type(exc).__name__})


def read_events(run_id, tenant_id, user_id, after):
    with SessionLocal() as db:
        run = get_run(db, tenant_id, run_id, user_id)
        if run is None:
            return [], {"type": "error", "message": "运行记录不可访问", "run_id": run_id}
        events = (db.query(AgentRunEvent).filter_by(run_id=run_id, tenant_id=tenant_id, kind="stream")
                  .filter(AgentRunEvent.id > after).order_by(AgentRunEvent.id).limit(100).all())
        rows = [(row.id, row.detail) for row in events]
        if len(rows) == 100 or run.status in ACTIVE:
            return rows, None
        result = run.data.get("result", {})
        if run.status == "cancelled":
            terminal = {"type": "cancelled", "message": "已停止，已有证据保留"}
        elif "answer" in result:
            terminal = {**result, "type": "final"}
        else:
            terminal = {"type": "error", "message": result.get("error") or "本次调查未完成，已有证据保留"}
        return rows, {**terminal, "run_id": run_id, "status": run.status}


async def subscribe(run_id, tenant_id, user_id, after=0):
    yield agent_service._sse({"type": "connected", "run_id": run_id})
    last_activity = time.monotonic()
    while True:
        rows, terminal = await run_in_threadpool(read_events, run_id, tenant_id, user_id, after)
        for after, event in rows:
            yield f"id: {after}\n" + agent_service._sse(event)
        if terminal:
            yield agent_service._sse(terminal)
            yield agent_service._sse({"type": "done"})
            return
        if rows:
            last_activity = time.monotonic()
        elif time.monotonic() - last_activity > 10:
            yield ": keep-alive\n\n"
            last_activity = time.monotonic()
        if len(rows) < 100:
            await asyncio.sleep(0.35)
