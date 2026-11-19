from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.container import get_container
from app.db.base import engine

router = APIRouter(tags=["health"])


@router.get("/live")
def live():
    return {"status":"ok", "app":get_settings().app_name}


@router.get("/ready")
def ready():
    from app.services.ingestion_service import worker_ready

    checks = {}
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"
    try:
        container = get_container()
        checks["vector_store"] = container.vector_store.health()
        redis = getattr(container.cache, "client", None)
        checks["cache"] = "ok" if redis and redis.ping() else "memory"
    except Exception:
        checks["dependencies"] = "unavailable"
    checks["ingestion_worker"] = "ok" if worker_ready() else "unavailable"
    healthy = "unavailable" not in checks.values()
    return JSONResponse({"status":"ready" if healthy else "not_ready", "checks":checks}, status_code=200 if healthy else 503)
