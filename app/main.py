"""FastAPI 应用入口：uvicorn app.main:app --reload"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import router_admin, router_agent, router_kb, router_session, router_ticket
from app.auth.router import router as auth_router
from app.auth.security import hash_password
from app.config import get_settings
from app.db.base import SessionLocal, init_db
from app.db.models import Tenant, User
from app.services import kb_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    from app.container import reset_container

    try:
        init_db()
        bootstrap()
        yield
    finally:
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
        chunk_total = kb_service.warm_indexes(db, container)
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

app.include_router(auth_router)
app.include_router(router_admin.router)
app.include_router(router_kb.router)
app.include_router(router_session.router)
app.include_router(router_agent.router)
app.include_router(router_ticket.router)


@app.get("/health")
def health():
    return {"status": "ok", "app": get_settings().app_name}


# 诊断工作台单页（同源挂载）。必须最后注册：通配 mount 会吞掉其后定义的所有路由。
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
