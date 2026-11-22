"""网站背景、提交证据与人工排障闭环聚合路由；不会访问用户服务器。"""

from fastapi import APIRouter

from app.api.support_cases import router as cases_router
from app.api.support_evidence import router as evidence_router
from app.api.support_library import router as library_router
from app.api.support_sites import router as sites_router
from app.api.support_sites import save_site
from app.services.support_report import case_dict

router = APIRouter(prefix="/api/support", tags=["personal-support"])
router.include_router(library_router)
router.include_router(sites_router)
router.include_router(cases_router)
router.include_router(evidence_router)

__all__ = ["case_dict", "router", "save_site"]
