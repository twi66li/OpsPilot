"""维护工作台概况、能力检查、站点探测与组织审批/工单操作详情。"""

from fastapi import HTTPException

from app.db.models import (
    AgentRun,
    ApprovalRequest,
    ChatSession,
    DocumentVersion,
    RemediationRequest,
    SupportCase,
    Ticket,
    WebsiteMonitor,
    WebsiteProfile,
    WorkspaceEvent,
)
from app.services.case_service import case_status, is_archived
from app.services.maintenance_timeline import record_event
from app.services.visitor_service import DEMO_RECORD_PREFIX


def site_summaries(db, user, sites):
    ids = [s.id for s in sites]
    rows = (
        db.query(SupportCase)
        .join(ChatSession)
        .filter(
            SupportCase.site_id.in_(ids),
            SupportCase.tenant_id == user.tenant_id,
            SupportCase.user_id == user.user_id,
            ChatSession.deleted_at.is_(None),
        )
        .all()
    )
    activity_rows = (
        db.query(WorkspaceEvent.resource_id, WorkspaceEvent.created_at)
        .filter(
            WorkspaceEvent.tenant_id == user.tenant_id,
            WorkspaceEvent.user_id == user.user_id,
            WorkspaceEvent.resource_id.in_([*ids, *[row.session_id for row in rows]]),
        )
        .all()
    )
    result = {}
    for site in sites:
        related = [r for r in rows if r.site_id == site.id]
        problems = [r for r in related if r.data.get("kind", "problem") == "problem"]
        evidence = [e for r in related for e in r.data.get("evidence", []) if e.get("kind") == "check_result"]
        doc_ids = site.data.get("doc_ids", [])
        versions = db.query(DocumentVersion.status).filter(DocumentVersion.doc_id.in_(doc_ids)).all() if doc_ids else []
        result[site.id] = {
            "problem_count": len(problems),
            "open_count": sum(case_status(r) != "user_resolved" and not is_archived(r) for r in problems),
            "last_check_at": max((e.get("submitted_at", "") for e in evidence), default=None),
            "last_activity_at": max(
                [
                    site.created_at.isoformat(),
                    *[r.updated_at.isoformat() for r in related],
                    *[
                        stamp.isoformat()
                        for resource, stamp in activity_rows
                        if resource == site.id or resource in {r.session_id for r in related}
                    ],
                ]
            ),
            "connection_state": "未接入实时监控",
            "health_check": site.data.get("health_check"),
            "document_tasks": {
                "processing": sum(s not in ("ready", "failed") for (s,) in versions),
                "failed": sum(s == "failed" for (s,) in versions),
            },
            "status_scope": "检查时间表示用户提交结果的时间；HTTP探测仅代表一次请求，不能证明网站整体恢复",
        }
    return result


def get_workspace_capabilities(db, user):
    from app.api.router_health import readiness_checks
    from app.config import get_settings

    settings = get_settings()
    checks = readiness_checks()
    scheduled = (
        db.query(WebsiteMonitor.site_id)
        .join(WebsiteProfile)
        .filter(
            WebsiteProfile.tenant_id == user.tenant_id,
            WebsiteProfile.user_id == user.user_id,
            WebsiteProfile.deleted_at.is_(None),
            WebsiteMonitor.enabled.is_(True),
        )
        .first()
    )
    configured = settings.llm_provider == "openai" and bool(settings.openai_api_key)
    knowledge_ready = checks.get("database") == "ok" and checks.get("vector_store") in ("ok", "memory")
    items = [
        {
            "key": "analysis",
            "title": "维护助手",
            "state": "configured" if configured else "offline",
            "description": "访客可以聊天和浏览教学记录；登录个人账号后可提交网站与日志进行排查。"
            if user.is_guest
            else "已配置聊天模型；这里未发起模型请求，实际可用性以分析结果为准。"
            if configured
            else "当前是离线模型模式，可以准备网站和日志，暂不能进行真实分析。",
        },
        {
            "key": "knowledge",
            "title": "资料与检索",
            "state": "available" if knowledge_ready else "unavailable",
            "description": "检索只使用可访问的官方资料和当前网站选中的私人手册；入库状态单独检查。",
        },
        {
            "key": "website",
            "title": "个人网站观测",
            "state": "available" if scheduled else "manual",
            "description": "已开启网站定时 HTTP 巡检。分析可结合巡检证据和提交的日志。"
            if scheduled
            else "可手动检查公开网址或提交日志。你也可以在网站详情开启定时 HTTP 巡检。",
        },
    ]
    if settings.drill_enabled:
        items.append(
            {
                "key": "demo",
                "title": "独立故障演练",
                "state": "configured" if settings.metrics_provider == "prometheus" else "offline",
                "description": "指标与日志配置仅用于平台自带的测试服务，与个人网站分开；配置存在不代表本次查询已成功。",
            }
        )
    runs_query = (
        db.query(AgentRun)
        .join(ChatSession, AgentRun.session_id == ChatSession.id)
        .filter(
            AgentRun.tenant_id == user.tenant_id,
            ChatSession.user_id == user.user_id,
            ChatSession.deleted_at.is_(None),
        )
    )
    if not settings.drill_enabled:
        runs_query = runs_query.join(SupportCase, SupportCase.session_id == ChatSession.id)
    runs = runs_query.order_by(AgentRun.updated_at.desc()).limit(5).all()
    reasons = {
        "model_error": "模型请求未完成",
        "budget_exceeded": "触及当前用量限制",
        "tool_error": "工具查询未完成",
        "answered": "已保存回答",
        "cancelled": "已停止",
        "interrupted": "服务重启中断了执行",
    }
    recent = []
    for run in runs:
        personal = db.get(SupportCase, run.session_id)
        reason = run.data.get("result", {}).get("end_reason") or run.status
        recent.append(
            {
                "id": run.id,
                "question": run.data.get("question", "未记录问题")[:100],
                "status": run.status,
                "reason": reasons.get(reason, "未记录具体结束原因"),
                "updated_at": run.updated_at.isoformat(),
                "url": ("/support/" if personal else "/investigation/") + run.session_id,
            }
        )
    return {"items": items, "checks": checks, "recent": recent}


def run_site_http_check(db, user, site):
    from app.container import get_container
    from app.services.cache import check_rate_limit
    from app.services.http_probe import probe_url

    if not site.data.get("url"):
        raise HTTPException(400, "请先在网站资料中填写公开网址")
    if not check_rate_limit(get_container().cache, "http-probe:" + user.user_id, 5):
        raise HTTPException(429, "检查过于频繁，请一分钟后重试")
    try:
        result = probe_url(site.data["url"])
    except (ValueError, OSError) as exc:
        raise HTTPException(400, "无法探测该地址：请确认公开HTTP(S)网址、端口与DNS；禁止访问内网") from exc
    history_point = {
        "collected_at": result.get("collected_at"),
        "elapsed_ms": result.get("elapsed_ms"),
        "http_status": result.get("http_status"),
        "state": result.get("state", "unknown"),
        "timed_out": bool(result.get("timed_out")),
        "source_kind": "read_only_http",
    }
    probe_history = [*site.data.get("probe_history", []), history_point][-20:]
    site.data = {**site.data, "health_check": result, "probe_history": probe_history}
    record_event(db, user.tenant_id, user.user_id, "site", site.id, "http_probe", result)
    db.commit()
    return result


def get_operation_detail(db, user, record_id):
    from app.config import get_settings
    from app.services.preflight import evaluate_remediation_preflight, evaluate_ticket_preflight
    from app.services.run_service import serialize_run

    if not user.is_guest and user.role not in ("admin", "engineer"):
        raise HTTPException(403, "需要工程师或管理员权限")
    if user.is_guest and not record_id.startswith(DEMO_RECORD_PREFIX):
        raise HTTPException(404, "审批或工单不存在")
    row = None
    kind = ""
    settings = get_settings()
    models = [(ApprovalRequest, "ticket_approval"), (Ticket, "ticket")]
    if settings.drill_enabled:
        models.append((RemediationRequest, "remediation"))
    for model, name in models:
        row = db.query(model).filter_by(id=record_id, tenant_id=user.tenant_id).first()
        if row:
            kind = name
            break
    if row is None:
        raise HTTPException(404, "审批或工单不存在")
    data = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    for key, value in data.items():
        if hasattr(value, "isoformat"):
            data[key] = value.isoformat()
    if user.is_guest:
        impact = "示例商城的跟进工单"
        if kind == "remediation":
            demo_run = db.get(AgentRun, row.run_id)
            if demo_run and demo_run.tenant_id == user.tenant_id and demo_run.data.get("demo"):
                data["session_id"] = demo_run.session_id
            impact = "关闭独立演练实例的故障注入"
        return {"kind": kind, "record": data, "run": None, "preflight": None, "impact": impact,
                "verification_scope": "公开演示样本"}
    runs = (
        db.query(AgentRun)
        .join(ChatSession, ChatSession.id == AgentRun.session_id)
        .outerjoin(SupportCase, SupportCase.session_id == AgentRun.session_id)
        .filter(
            AgentRun.tenant_id == user.tenant_id,
            ChatSession.tenant_id == user.tenant_id,
            ChatSession.deleted_at.is_(None),
            SupportCase.session_id.is_(None),
        )
    )
    if kind == "remediation":
        run = runs.filter(AgentRun.id == row.run_id).first()
    else:
        run = runs.filter(AgentRun.session_id == row.session_id).order_by(AgentRun.created_at.desc()).first()
    preflight = None
    if kind == "remediation":
        preflight = evaluate_remediation_preflight(
            row.action,
            service=row.service,
            tenant_id=row.tenant_id,
            requested_by=row.requested_by,
            approver_username=user.username,
            approver_role=user.role,
            remediation_enabled=settings.drill_remediation_enabled,
            strict_four_eyes=settings.strict_four_eyes,
        )
    elif kind == "ticket_approval":
        preflight = evaluate_ticket_preflight(
            row.ticket_draft,
            requested_by=row.requested_by,
            approver_username=user.username,
            approver_role=user.role,
            strict_four_eyes=settings.strict_four_eyes,
        )
    # 这是组织审批入口，按本租户工程师权限读取对应演练调查，不开放个人排障记录。
    return {
        "kind": kind,
        "record": data,
        "run": serialize_run(db, run) if run else None,
        "preflight": preflight,
        "impact": f"将关闭 {row.service} 测试服务的故障注入"
        if kind == "remediation"
        else "仅创建或查看跟进工单；不执行修复命令",
        "verification_scope": "验证仅对应所记录的请求样本，不能证明生产根因消除或长期恢复",
    }
