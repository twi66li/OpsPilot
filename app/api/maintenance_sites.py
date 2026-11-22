"""维护网站观测：变更记录、站点时间线、HTTP 探测、定时巡检与 Webhook 告警接收。"""

from datetime import timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import AuthUser, get_current_user
from app.db.base import get_db
from app.db.models import WebsiteProfile
from app.services import website_monitor
from app.services.maintenance_timeline import build_site_timeline, record_event
from app.services.maintenance_workspace import run_site_http_check
from app.services.support_evidence import owned_site, redact

router = APIRouter()


class ChangeIn(BaseModel):
    change_kind: Literal["release", "config", "database", "maintenance"]
    title: str = Field(min_length=1, max_length=100)
    occurred_at: AwareDatetime
    version: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=1500)
    rollback: str = Field(default="", max_length=600)


@router.post("/sites/{site_id}/changes", status_code=201)
def add_change(site_id: str, body: ChangeIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    owned_site(db, user, site_id)
    detail = {key: redact(value)["text"] for key, value in body.model_dump(exclude={"occurred_at"}).items()}
    detail.update(
        occurred_at=body.occurred_at.astimezone(timezone.utc).isoformat(),
        actor=user.username,
        source="用户手工记录；时间接近不代表因果关系",
    )
    record_event(db, user.tenant_id, user.user_id, "site", site_id, "site_change", detail)
    db.commit()
    return {"recorded": True}


@router.get("/sites/{site_id}/timeline")
def site_timeline(site_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    owned_site(db, user, site_id)
    return build_site_timeline(db, user, site_id)


@router.post("/sites/{site_id}/http-check")
def http_check(site_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    site = owned_site(db, user, site_id)
    return run_site_http_check(db, user, site)


@router.post("/sites/{site_id}/webhook-token")
def rotate_site_webhook_token(
    site_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)
):
    from app.services.webhook_service import rotate_token

    site = owned_site(db, user, site_id)
    return rotate_token(db, site)


@router.delete("/sites/{site_id}/webhook-token")
def revoke_site_webhook_token(
    site_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)
):
    from app.services.webhook_service import revoke_token

    site = owned_site(db, user, site_id)
    return revoke_token(db, site)


@router.post("/sites/{site_id}/webhook")
async def receive_site_webhook(
    site_id: str,
    request: Request,
    x_opspilot_webhook_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """低门槛外部告警接收入口（支持 Alertmanager / Uptime Kuma / 通用 JSON）。"""
    import json

    from app.container import get_container
    from app.services.cache import check_rate_limit
    from app.services.webhook_service import ingest_webhook_alert, verify_token

    site = db.query(WebsiteProfile).filter_by(id=site_id, deleted_at=None).first()
    token = x_opspilot_webhook_token or ""
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if site is None or not verify_token(site, token):
        raise HTTPException(401, "Webhook 令牌无效或未启用")
    if not check_rate_limit(get_container().cache, "site-webhook:" + site.id, 20):
        raise HTTPException(429, "该站点告警接收过于频繁，请一分钟后重试")
    raw = await request.body()
    if len(raw) > 65536:
        raise HTTPException(413, "告警载荷过大，最大支持 64KB")
    try:
        payload = json.loads(raw.decode("utf-8-sig")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "告警载荷必须是合法的 UTF-8 JSON 对象") from exc
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(400, "告警载荷必须是非空 JSON 对象")
    return ingest_webhook_alert(db, site, payload)


class HttpMonitorIn(BaseModel):
    enabled: bool
    interval_minutes: Literal[5, 15, 30, 60] = 15


@router.put("/sites/{site_id}/http-monitor")
def configure_http_monitor(
    site_id: str, body: HttpMonitorIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)
):
    site = owned_site(db, user, site_id)
    try:
        return website_monitor.configure(db, site, body.enabled, body.interval_minutes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
