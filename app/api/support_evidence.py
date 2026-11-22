"""排障证据接收与检查方案闭环路由：证据预览、提交、方案确认与步骤打卡。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import AuthUser, get_current_user
from app.db.base import get_db
from app.services.case_service import assert_editable
from app.services.evidence_checks import summarize_evidence
from app.services.maintenance_timeline import record_event
from app.services.support_evidence import EvidenceIn, new_evidence, owned_case, redact
from app.services.support_report import case_dict

router = APIRouter()


@router.post("/evidence/preview")
def preview(body: EvidenceIn, user: AuthUser = Depends(get_current_user)):
    result = redact(body.text)
    return {
        **result,
        "summary": summarize_evidence({"id": "preview", "kind": body.kind, "text": result["text"], "endpoint": body.endpoint}),
    }


@router.post("/cases/{session_id}/evidence")
def submit_evidence(
    session_id: str, body: EvidenceIn, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_db)
):
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
    record_event(
        db,
        user.tenant_id,
        user.user_id,
        "case",
        session_id,
        "evidence_submitted",
        {"evidence_id": item["id"], "label": item["label"], "kind": item["kind"]},
    )
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
        case.data = {
            **case.data,
            "plan": {
                "revision": revision,
                "steps": report["steps"],
                "evidence_ids": [e["id"] for e in case.data.get("evidence", [])],
                "checks": [{"status": "pending", "note": ""} for _ in report["steps"]],
            },
        }
        case.status = "awaiting_results"
        record_event(
            db,
            user.tenant_id,
            user.user_id,
            "case",
            session_id,
            "plan_confirmed",
            {"revision": revision, "actor": user.username},
        )
        db.commit()
    return {
        "status": case.status,
        "message": "已记录方案确认。请在自己的网站环境操作，再提交新的检查结果；本平台未执行任何操作。",
    }


class StepIn(BaseModel):
    revision: int
    status: str = "done"
    note: str = Field(default="", max_length=2000)


@router.post("/cases/{session_id}/plan/steps/{step_index}")
def update_step(
    session_id: str,
    step_index: int,
    body: StepIn,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
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
    record_event(
        db,
        user.tenant_id,
        user.user_id,
        "case",
        session_id,
        "step_updated",
        {
            "revision": body.revision,
            "step": step_index + 1,
            "status": body.status,
            "note": checks[step_index]["note"],
            "actor": user.username,
        },
    )
    db.commit()
    return case_dict(case)
