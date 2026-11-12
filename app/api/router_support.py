"""网站背景、提交证据与人工排障闭环；不会访问用户服务器。"""
from dataclasses import replace

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import AuthUser, get_current_user
from app.db.base import get_db
from app.db.models import Document, SupportCase, Tenant, WebsiteProfile
from app.services import chat_service
from app.services.case_service import CASE_STATUSES, assert_editable, case_status, is_archived, list_cases, reopen
from app.services.evidence_checks import analyze_samples
from app.services.knowledge_access import personal_namespace
from app.services.official_library import BY_ID, SCENARIOS, catalog
from app.services.support_service import (
    CaseIn,
    EvidenceIn,
    SiteIn,
    new_evidence,
    owned_case,
    owned_site,
    redact,
    validate_private_docs,
)

router = APIRouter(prefix="/api/support", tags=["personal-support"])


def case_dict(case):
    return {**case.data, "id": case.session_id, "site_id": case.site_id, "status": case_status(case),
            "archived": is_archived(case), "saved": is_archived(case), "kind": case.data.get("kind", "problem"),
            "sample_checks": analyze_samples(case.data), "created_at": case.created_at.isoformat()}


@router.get("/library")
def library(user: AuthUser = Depends(get_current_user)):
    return catalog()


@router.get("/scenarios")
def scenarios(user: AuthUser = Depends(get_current_user)):
    return [{**guide, "source": BY_ID[guide["doc_id"]]["url"]} for guide in SCENARIOS]


@router.get("/library/{doc_id}")
def library_document(doc_id: str, user: AuthUser = Depends(get_current_user)):
    item = BY_ID.get(doc_id)
    if not item:
        raise HTTPException(404, "资料不存在")
    return {"doc_id": doc_id, "doc_title": item["title"], "text": item["content"], "parent_text": item["content"],
            "source": item["url"], "source_kind": "official", "applicable_version": item["applicable_version"],
            "checked_at": item["checked_at"]}


@router.get("/private-documents")
def private_documents(user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    docs = db.query(Document).filter_by(tenant_id=personal_namespace(user.user_id), created_by=user.user_id).all()
    return [{"id": d.id, "title": d.title, "source": d.source, "chunk_count": d.chunk_count,
             "active_version_id": d.active_version_id} for d in docs]


@router.post("/documents/upload", status_code=202)
def upload_personal(file: UploadFile = File(...), title: str = Form(""), doc_id: str = Form(""),
                    parsed_file: UploadFile | None = File(None), user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.api.router_kb import upload_document

    namespace = personal_namespace(user.user_id)
    if not db.get(Tenant, namespace):
        db.add(Tenant(id=namespace, name="个人网站手册"))
        db.commit()
    return upload_document(file, title, doc_id, parsed_file, replace(user, tenant_id=namespace), db)


@router.get("/sites")
def sites(user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    records = list_cases(db, user, kind="problem")
    return [{"id": s.id, **s.data, "problem_count": sum(c["site_id"] == s.id for c in records),
             "open_count": sum(c["site_id"] == s.id and c["status"] != "user_resolved" and not c["archived"] for c in records)}
            for s in db.query(WebsiteProfile).filter_by(tenant_id=user.tenant_id, user_id=user.user_id).all()]


@router.get("/sites/{site_id}")
def site_detail(site_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    site = owned_site(db, user, site_id)
    docs = db.query(Document).filter(Document.id.in_(site.data.get("doc_ids", [])),
                                     Document.tenant_id == personal_namespace(user.user_id)).all()
    return {"id": site.id, **site.data, "documents": [{"id": d.id, "title": d.title,
            "chunk_count": d.chunk_count, "active_version_id": d.active_version_id} for d in docs],
            "cases": list_cases(db, user, site_id=site_id, kind="problem")}


def save_site(db, user, body, site=None):
    validate_private_docs(db, user, body.doc_ids)
    data = {key: redact(value)["text"] if isinstance(value, str) else value for key, value in body.model_dump().items()}
    site = site or WebsiteProfile(tenant_id=user.tenant_id, user_id=user.user_id)
    site.data = data
    db.add(site)
    db.commit()
    return {"id": site.id, **site.data}


@router.post("/sites")
def create_site(body: SiteIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return save_site(db, user, body)


@router.put("/sites/{site_id}")
def update_site(site_id: str, body: SiteIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return save_site(db, user, body, owned_site(db, user, site_id))


@router.post("/cases")
def create_case(body: CaseIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    site = owned_site(db, user, body.site_id) if body.site_id else None
    session = chat_service.create_session(db, user.tenant_id, user.user_id, redact(body.title)["text"])
    case = SupportCase(session_id=session.id, tenant_id=user.tenant_id, user_id=user.user_id,
                       site_id=site.id if site else None,
                       data={"profile": site.data if site else {}, "evidence": [], "revision": 0, "kind": body.kind,
                             "problem": {key: redact(getattr(body, key))["text"] for key in
                                         ("symptom", "endpoint", "impact", "started_at", "recent_changes")}})
    db.add(case)
    db.commit()
    return case_dict(case)


@router.get("/cases")
def cases(site_id: str | None = None, kind: str | None = None, status: str | None = None, archived: bool | None = None,
          user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return list_cases(db, user, site_id, kind, status, archived)


class ProblemIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    symptom: str = Field(default="", max_length=2000)
    endpoint: str = Field(default="", max_length=200)
    impact: str = Field(default="", max_length=500)
    started_at: str = Field(default="", max_length=100)
    recent_changes: str = Field(default="", max_length=1000)


@router.put("/cases/{session_id}/problem")
def update_problem(session_id: str, body: ProblemIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    case = owned_case(db, user, session_id)
    assert_editable(case)
    if body.title is not None:
        session = chat_service.get_session(db, user.tenant_id, session_id, user.user_id)
        session.title = redact(body.title)["text"]
    case.data = {**case.data, "kind": "problem", "problem": {k: redact(v)["text"] for k, v in body.model_dump(exclude={"title"}).items()}}
    db.commit()
    return case_dict(case)


class StatusIn(BaseModel):
    status: str
    note: str = Field(default="", max_length=2000)
    user_confirmed: bool = False


@router.post("/cases/{session_id}/status")
def change_status(session_id: str, body: StatusIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    case = owned_case(db, user, session_id)
    if body.status == "reopen":
        reopen(case)
    else:
        assert_editable(case)
        if body.status not in CASE_STATUSES:
            raise HTTPException(400, "未知的问题状态")
        if body.status == "user_resolved" and (not body.user_confirmed or not body.note.strip()):
            raise HTTPException(400, "确认解决需要本人确认并填写实际处理记录")
        if body.status == "sample_recovered":
            raise HTTPException(400, "样本核对状态由程序计算，不能手动指定")
        case.status = body.status
    case.data = {**case.data, "status_note": redact(body.note)["text"]}
    if body.status == "user_resolved":
        case.data = {**case.data, "solution": redact(body.note)["text"], "user_confirmed_resolved": True}
    db.commit()
    return case_dict(case)


@router.get("/cases/{session_id}")
def get_case(session_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return case_dict(owned_case(db, user, session_id))


@router.post("/evidence/preview")
def preview(body: EvidenceIn, user: AuthUser = Depends(get_current_user)):
    return redact(body.text)


@router.post("/cases/{session_id}/evidence")
def submit_evidence(session_id: str, body: EvidenceIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    case = owned_case(db, user, session_id)
    assert_editable(case)
    evidence = case.data.get("evidence", [])
    if len(evidence) >= 12 or sum(len(e["text"]) for e in evidence) + len(body.text) > 30000:
        raise HTTPException(400, "本会话证据已达上限，请为新的问题创建会话")
    if body.kind == "check_result" and not case.data.get("plan"):
        raise HTTPException(409, "请先确认当前检查方案；普通补充日志请选择初始/补充证据")
    item = new_evidence(body)
    if body.kind == "check_result":
        plan = case.data["plan"]
        if body.plan_revision is not None and body.plan_revision != plan["revision"]:
            raise HTTPException(409, "检查方案已经更新，请刷新后提交")
        if body.step_index is not None and body.step_index >= len(plan["steps"]):
            raise HTTPException(400, "检查步骤不存在")
        item["plan_revision"] = plan["revision"]
    case.data = {**case.data, "kind": "problem", "evidence": [*evidence, item]}
    case.status = "evidence_submitted"
    db.commit()
    return item


@router.post("/cases/{session_id}/confirm-plan")
def confirm_plan(session_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    case = owned_case(db, user, session_id)
    assert_editable(case)
    report = case.data.get("report") or {}
    if not report.get("steps"):
        raise HTTPException(409, "当前没有可确认的检查方案")
    revision = case.data["revision"]
    # 重复确认同一方案不重置证据基线。
    if case.data.get("plan", {}).get("revision") != revision:
        case.data = {**case.data, "plan": {"revision": revision, "steps": report["steps"],
                                           "evidence_ids": [e["id"] for e in case.data.get("evidence", [])],
                                           "checks": [{"status": "pending", "note": ""} for _ in report["steps"]]}}
        case.status = "awaiting_results"
        db.commit()
    return {"status": case.status, "message": "已记录方案确认。请在自己的网站环境操作，再提交新的检查结果；本平台未执行任何操作。"}


class StepIn(BaseModel):
    revision: int
    status: str = "done"
    note: str = Field(default="", max_length=2000)


@router.post("/cases/{session_id}/plan/steps/{step_index}")
def update_step(session_id: str, step_index: int, body: StepIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    case = owned_case(db, user, session_id)
    assert_editable(case)
    plan = dict(case.data.get("plan", {}))
    if plan.get("revision") != body.revision or not 0 <= step_index < len(plan.get("steps", [])):
        raise HTTPException(409, "检查方案或步骤已变化，请刷新后重试")
    if body.status not in ("pending", "done", "skipped"):
        raise HTTPException(400, "未知的步骤状态")
    checks = [*plan.get("checks", [{"status": "pending", "note": ""} for _ in plan["steps"]])]
    checks[step_index] = {"status": body.status, "note": redact(body.note)["text"]}
    plan["checks"] = checks
    case.data = {**case.data, "plan": plan}
    db.commit()
    return case_dict(case)


class SaveIn(BaseModel):
    solution: str = Field(min_length=1, max_length=2000)
    user_confirmed: bool = False


@router.post("/cases/{session_id}/save")
def save_case(session_id: str, body: SaveIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)):
    case = owned_case(db, user, session_id)
    if is_archived(case):
        return case_dict(case)
    if not body.user_confirmed:
        raise HTTPException(400, "保存案例需要本人确认处理记录")
    if not case.data.get("report"):
        raise HTTPException(409, "请先完成一次排障分析")
    case.data = {**case.data, "saved": True, "archived": True, "solution": redact(body.solution)["text"],
                 "result_label": "用户确认的处理记录；恢复结论以报告中的证据范围为限"}
    db.commit()
    return case_dict(case)
