"""工作台分页协议；旧列表保留给既有脚本，界面使用服务端分页与搜索。"""

from typing import Generic, TypeVar

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Text, case, cast, func, or_
from sqlalchemy.orm import Session

from app.auth.deps import AuthUser, get_current_user
from app.db.base import get_db
from app.db.models import (
    ApprovalRequest,
    ChatSession,
    Document,
    RemediationRequest,
    SupportCase,
    Ticket,
    WebsiteProfile,
)
from app.services.case_service import case_item
from app.services.knowledge_access import personal_namespace

router = APIRouter(prefix="/api/workspace", tags=["workspace"])
T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


class CaseItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    title: str
    status: str
    severity: str
    archived: bool
    created_at: str


class SiteItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    name: str
    doc_ids: list[str]


class SessionItem(BaseModel):
    id: str
    title: str
    created_at: str


class PageQuery(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    q: str = Field(default="", max_length=200)
    site_id: str | None = None
    status: str | None = None
    case_kind: str | None = None
    archived: bool | None = None
    severity: str | None = None


def read_page(kind, params, user, db):
    if kind == "cases":
        query = (
            db.query(SupportCase, ChatSession)
            .join(ChatSession)
            .filter(
                SupportCase.tenant_id == user.tenant_id,
                SupportCase.user_id == user.user_id,
                ChatSession.deleted_at.is_(None),
            )
        )
        if params.site_id:
            query = query.filter(SupportCase.site_id == params.site_id)
        if params.case_kind:
            query = query.filter(func.coalesce(SupportCase.data["kind"].as_string(), "problem") == params.case_kind)
        if params.severity:
            query = query.filter(SupportCase.severity == params.severity)
        archived = or_(
            SupportCase.status == "saved",
            SupportCase.data["archived"].as_boolean().is_(True),
            SupportCase.data["saved"].as_boolean().is_(True),
        )
        if params.archived is not None:
            query = query.filter(archived.is_(params.archived))
        normalized = case(
            (
                SupportCase.status == "saved",
                case(
                    (
                        SupportCase.data["report"]["verification"]["verdict"].as_string() == "sample_recovered",
                        "sample_recovered",
                    ),
                    else_="investigating",
                ),
            ),
            else_=SupportCase.status,
        )
        if params.status:
            query = query.filter(normalized == params.status)
        if params.q:
            query = query.filter(
                or_(
                    ChatSession.title.icontains(params.q, autoescape=True),
                    SupportCase.data["problem"]["symptom"].as_string().icontains(params.q, autoescape=True),
                    SupportCase.data["report"]["summary"].as_string().icontains(params.q, autoescape=True),
                    cast(SupportCase.data, Text).icontains(params.q, autoescape=True),
                    cast(SupportCase.labels, Text).contains(params.q, autoescape=True),
                )
            )
        query = query.order_by(SupportCase.created_at.desc(), SupportCase.session_id.desc())

        def serialize(row):
            return case_item(*row)
    elif kind == "sites":
        query = db.query(WebsiteProfile).filter_by(tenant_id=user.tenant_id, user_id=user.user_id, deleted_at=None)
        if params.q:
            query = query.filter(
                or_(
                    *[
                        WebsiteProfile.data[key].as_string().icontains(params.q, autoescape=True)
                        for key in ("name", "url", "framework", "deployment", "database", "versions", "notes")
                    ]
                )
            )
        query = query.order_by(WebsiteProfile.created_at.desc(), WebsiteProfile.id.desc())

        def serialize(row):
            return {"id": row.id, **row.data}
    elif kind == "sessions":
        personal = db.query(SupportCase.session_id).filter_by(tenant_id=user.tenant_id, user_id=user.user_id)
        query = db.query(ChatSession).filter(
            ChatSession.tenant_id == user.tenant_id,
            ChatSession.user_id == user.user_id,
            ChatSession.deleted_at.is_(None),
            ChatSession.id.notin_(personal),
        )
        if params.q:
            query = query.filter(ChatSession.title.icontains(params.q, autoescape=True))
        query = query.order_by(ChatSession.created_at.desc(), ChatSession.id.desc())

        def serialize(row):
            return {"id": row.id, "title": row.title, "created_at": row.created_at.isoformat()}
    elif kind == "documents":
        query = db.query(Document).filter_by(
            tenant_id=personal_namespace(user.user_id), created_by=user.user_id, deleted_at=None
        )
        if params.q:
            query = query.filter(Document.title.icontains(params.q, autoescape=True))
        query = query.order_by(Document.created_at.desc(), Document.id.desc())

        def serialize(row):
            return {
                "id": row.id,
                "title": row.title,
                "source": row.source,
                "chunk_count": row.chunk_count,
                "active_version_id": row.active_version_id,
            }
    elif kind in ("approvals", "tickets", "remediations"):
        if user.role not in ("admin", "engineer"):
            raise HTTPException(403, "需要工程师或管理员权限")
        model = {"approvals": ApprovalRequest, "tickets": Ticket, "remediations": RemediationRequest}[kind]
        query = db.query(model).filter_by(tenant_id=user.tenant_id)
        if params.status:
            query = query.filter(model.status == params.status)
        if params.q:
            field = (
                model.title
                if hasattr(model, "title")
                else model.reason
                if hasattr(model, "reason")
                else cast(model.ticket_draft, Text)
            )
            query = query.filter(field.icontains(params.q, autoescape=True))
        query = query.order_by(model.created_at.desc(), model.id.desc())

        def serialize(row):
            data = {c.name: getattr(row, c.name) for c in model.__table__.columns}
            for key, value in data.items():
                if hasattr(value, "isoformat"):
                    data[key] = value.isoformat()
            if kind == "approvals":
                data["approval_id"] = row.id
            if kind == "remediations":
                data["action_label"] = "关闭演练故障注入"
            return data
    else:
        raise HTTPException(404, "列表不存在")
    total = query.order_by(None).count()
    rows = query.offset((params.page - 1) * params.page_size).limit(params.page_size).all()
    if kind == "sites":
        from app.services.maintenance_service import site_summaries

        summaries = site_summaries(db, user, rows)

        def serialize(row):
            return {"id": row.id, **row.data, **summaries[row.id]}

    return {
        "items": [serialize(row) for row in rows],
        "total": total,
        "page": params.page,
        "page_size": params.page_size,
    }


@router.get("/cases", response_model=PageResult[CaseItem])
def cases(params: PageQuery = Depends(), user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return read_page("cases", params, user, db)


@router.get("/sites", response_model=PageResult[SiteItem])
def sites(params: PageQuery = Depends(), user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return read_page("sites", params, user, db)


@router.get("/sessions", response_model=PageResult[SessionItem])
def sessions(params: PageQuery = Depends(), user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return read_page("sessions", params, user, db)


@router.get("/{kind}", response_model=PageResult[dict])
def other(
    kind: str, params: PageQuery = Depends(), user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)
):
    return read_page(kind, params, user, db)
