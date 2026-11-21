import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.container import get_container
from app.db.base import engine

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/live")
def live():
    return {"status":"ok", "app":get_settings().app_name}


def readiness_checks():
    from app.services.ingestion_service import worker_ready

    checks = {}
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        logger.warning("readiness_database_failed error_type=%s", type(exc).__name__)
        checks["database"] = "unavailable"
    try:
        container = get_container()
        checks["vector_store"] = container.vector_store.health()
        redis = getattr(container.cache, "client", None)
        checks["cache"] = "ok" if redis and redis.ping() else "memory"
    except Exception as exc:
        logger.warning("readiness_dependencies_failed error_type=%s", type(exc).__name__)
        checks["dependencies"] = "unavailable"
    checks["ingestion_worker"] = "ok" if worker_ready() else "unavailable"
    return checks


@router.get("/ready")
def ready():
    checks = readiness_checks()
    healthy = "unavailable" not in checks.values()
    return JSONResponse({"status":"ready" if healthy else "not_ready", "checks":checks}, status_code=200 if healthy else 503)
