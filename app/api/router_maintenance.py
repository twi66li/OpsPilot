"""维护工作台聚合路由：按工作台、网站巡检、案例记录、交接与工单四个子模块注册。"""

from fastapi import APIRouter

from app.api.maintenance_cases import router as cases_router
from app.api.maintenance_handoff import router as handoff_router
from app.api.maintenance_sites import router as sites_router
from app.api.maintenance_workspace import router as workspace_router

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])
router.include_router(workspace_router)
router.include_router(cases_router)
router.include_router(sites_router)
router.include_router(handoff_router)
