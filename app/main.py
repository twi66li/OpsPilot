"""FastAPI 应用入口：uvicorn app.main:app --reload"""
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    router_admin,
    router_agent,
    router_eval,
    router_kb,
    router_remediation,
    router_session,
    router_support,
    router_ticket,
)
from app.auth.router import router as auth_router
from app.auth.security import hash_password
from app.config import get_settings
from app.db.base import SessionLocal, init_db
from app.db.models import RemediationRequest, Tenant, User
from app.services import kb_service
from app.services.logging_setup import configure_logging

configure_logging()


@asynccontextmanager
async def lifespan(_: FastAPI):
    from app.container import reset_container

    try:
        init_db()
        bootstrap()
        from app.services.ingestion_service import start_worker

        start_worker()
        yield
    finally:
        from app.db.async_session import close_async_db
        from app.services.ingestion_service import stop_worker

        stop_worker()
        await close_async_db()
        reset_container()


def bootstrap() -> None:
    """初始化默认租户、管理员账号，并基于 MySQL 全量文档重建 BM25 索引。"""
    settings = get_settings()
    with SessionLocal() as db:
        if not db.get(Tenant, "default"):
            db.add(Tenant(id="default", name="默认租户"))
            db.commit()
        if not db.query(User).filter(User.username == settings.admin_username).first():
            db.add(
                User(
                    tenant_id="default",
                    username=settings.admin_username,
                    password_hash=hash_password(settings.admin_password),
                    role="admin",
                )
            )
            db.commit()
        container = _build_container()
        db.query(RemediationRequest).filter_by(status="running").update(
            {"status": "interrupted", "result": {"message": "服务重启中断了执行，外部最终状态需重新调查核对"}},
            synchronize_session=False)
        db.commit()
        chunk_total = kb_service.warm_indexes(db, container)
        from app.services.official_library import ensure_library

        ensure_library(db, container)
        logging.getLogger(__name__).info("启动完成：索引已就绪，共 %d 个分块", chunk_total)


def _build_container():
    from app.container import get_container

    return get_container()


app = FastAPI(
    title="AIOps 智能故障诊断 Agent",
    description="混合检索（Qdrant + BM25 + RRF + 重排）× LangGraph 诊断工作流 × 租户隔离 × SSE 流式 × 人工审批",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    request_id = uuid.uuid4().hex
    structlog.contextvars.bind_contextvars(request_id=request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        structlog.get_logger("http").info("response_started", method=request.method, path=request.url.path,
                                          status=response.status_code,
                                          elapsed_ms=round((time.perf_counter() - started) * 1000, 1))
        return response
    finally:
        structlog.contextvars.clear_contextvars()

app.include_router(auth_router)
app.include_router(router_admin.router)
app.include_router(router_kb.router)
app.include_router(router_session.router)
app.include_router(router_agent.router)
app.include_router(router_eval.router)
app.include_router(router_ticket.router)
app.include_router(router_remediation.router)
app.include_router(router_support.router)


@app.get("/health")
def health():
    return {"status": "ok", "app": get_settings().app_name}


# 诊断工作台单页（同源挂载）。必须最后注册：通配 mount 会吞掉其后定义的所有路由。
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
else:
    @app.get("/", response_class=HTMLResponse, status_code=503, include_in_schema=False)
    def frontend_not_built():
        return '<html lang="zh-CN"><h1>OpsPilot</h1><p>请在frontend目录执行 npm ci 和 npm run build，随后重启API。</p></html>'
