"""个人网站背景资料路由：列表、详情、保存与删除。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.deps import AuthUser, get_current_user
from app.db.base import get_db
from app.db.models import ChatSession, Document, SupportCase, WebsiteMonitor, WebsiteProfile, _now
from app.services import chat_service
from app.services.case_service import list_cases
from app.services.knowledge_access import personal_namespace
from app.services.maintenance_timeline import record_event
from app.services.maintenance_workspace import site_summaries
from app.services.support_evidence import SiteIn, owned_site, redact, validate_private_docs
from app.services.website_monitor import monitor_info

router = APIRouter()


@router.get("/sites")
def sites(user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services.webhook_service import public_site_data

    rows = db.query(WebsiteProfile).filter_by(tenant_id=user.tenant_id, user_id=user.user_id, deleted_at=None).all()
    summaries = site_summaries(db, user, rows)
    return [{"id": site.id, **public_site_data(site.data), **summaries[site.id]} for site in rows]


@router.get("/sites/{site_id}")
def site_detail(site_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services.webhook_service import public_site_data

    site = owned_site(db, user, site_id)
    docs = (
        db.query(Document)
        .filter(Document.id.in_(site.data.get("doc_ids", [])), Document.tenant_id == personal_namespace(user.user_id))
        .all()
    )
    return {
        "id": site.id,
        **public_site_data(site.data),
        "http_monitor": monitor_info(db.get(WebsiteMonitor, site.id)),
        **site_summaries(db, user, [site])[site.id],
        "documents": [
            {"id": d.id, "title": d.title, "chunk_count": d.chunk_count, "active_version_id": d.active_version_id}
            for d in docs
        ],
        "cases": list_cases(db, user, site_id=site_id, kind="problem"),
    }


def save_site(db, user, body, site=None):
    validate_private_docs(db, user, body.doc_ids)
    data = {
        key: redact(value)["text"] if isinstance(value, str) else value
        for key, value in body.model_dump(exclude_unset=site is not None).items()
    }
    if "dependencies" in data:
        data["dependencies"] = [
            {key: redact(value)["text"] for key, value in relation.items()} for relation in data["dependencies"]
        ]
    site = site or WebsiteProfile(tenant_id=user.tenant_id, user_id=user.user_id)
    site.data = {**(site.data or {}), **data}
    db.add(site)
    db.flush()
    record_event(
        db,
        user.tenant_id,
        user.user_id,
        "site",
        site.id,
        "site_saved",
        {"name": site.data.get("name"), "actor": user.username},
    )
    db.commit()
    return {"id": site.id, **site.data}


@router.post("/sites")
def create_site(body: SiteIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return save_site(db, user, body)


@router.put("/sites/{site_id}")
def update_site(site_id: str, body: SiteIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return save_site(db, user, body, owned_site(db, user, site_id))


@router.delete("/sites/{site_id}")
def delete_site(
    site_id: str,
    delete_records: bool = False,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    site = owned_site(db, user, site_id)
    records = (
        db.query(SupportCase)
        .join(ChatSession, ChatSession.id == SupportCase.session_id)
        .filter(
            SupportCase.site_id == site_id,
            SupportCase.tenant_id == user.tenant_id,
            SupportCase.user_id == user.user_id,
            ChatSession.deleted_at.is_(None),
        )
        .all()
    )
    if delete_records and records:
        chat_service.delete_sessions(db, user.tenant_id, user.user_id, [c.session_id for c in records])
    for case in records:
        case.site_id = None  # 历史环境快照保留，原手册不随网站删除。
    site.deleted_at = _now()
    db.commit()
    return {"deleted": 1, "deleted_records": len(records) if delete_records else 0}
