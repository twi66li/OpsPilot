"""公开只读体验账号；示例记录只属于专用租户，不读取或复制日常数据。"""

from datetime import datetime

from app.auth.security import hash_password
from app.db.models import ChatSession, Message, SupportCase, Tenant, User, WebsiteProfile, WorkspaceEvent
from app.services.maintenance_service import record_event

VISITOR_ID = "opspilot-public-visitor"
VISITOR_TENANT = "opspilot-public-demo"
VISITOR_USERNAME = "visitor"
# 公开体验口令，不用于任何真实账号或外部服务。
VISITOR_PASSWORD = "OpsPilotDemo2026!"


def is_visitor(user):
    return user.id == VISITOR_ID and user.tenant_id == VISITOR_TENANT


def ensure_visitor_context(db, site):
    # 只给专用教学网站补充新示例，不更新普通用户的网站或历史问题快照。
    defaults = {"environment": "test", "group": "示例业务", "owner": "示例维护人",
                "dependencies": [{"source": "Nginx", "target": "app", "note": "转发结算请求"},
                                 {"source": "app", "target": "PostgreSQL", "note": "读取订单数据"}]}
    site.data = {**defaults, **site.data}
    exists = db.query(WorkspaceEvent.id).filter_by(tenant_id=VISITOR_TENANT, user_id=VISITOR_ID,
                                                 resource_type="site", resource_id=site.id, kind="site_change").first()
    if not exists:
        record_event(db, VISITOR_TENANT, VISITOR_ID, "site", site.id, "site_change",
                     {"change_kind": "release", "title": "教学示例：发布结算服务 v2", "version": "v2",
                      "occurred_at": "2026-11-01T01:55:00+00:00", "actor": "教学示例",
                      "note": "与日志时间对照的教学记录，不证明发布是根因。", "source": "预置教学示例"})


def ensure_visitor(db):
    existing = db.query(User).filter_by(username=VISITOR_USERNAME).first()
    if existing and not is_visitor(existing):
        raise RuntimeError("visitor 名称已被普通账号使用，不能覆盖为公开访客账号")
    if not db.get(Tenant, VISITOR_TENANT):
        db.add(Tenant(id=VISITOR_TENANT, name="公开教学示例"))
        db.flush()
    if not existing:
        db.add(User(id=VISITOR_ID, tenant_id=VISITOR_TENANT, username=VISITOR_USERNAME,
                    password_hash=hash_password(VISITOR_PASSWORD), role="viewer"))
        db.flush()
    previous = db.get(WebsiteProfile, "visitor-example-site")
    if previous:
        ensure_visitor_context(db, previous)
        db.commit()
        return
    at = datetime(2026, 11, 1, 2, 0)
    profile = {"name": "示例商城（教学数据）", "url": "", "framework": "FastAPI + Nginx",
               "deployment": "Docker Compose", "database": "PostgreSQL 16", "versions": "Python 3.12",
               "doc_ids": [], "notes": "公开教学示例，用于体验排障流程；不是接入中的真实网站。"}
    site = WebsiteProfile(id="visitor-example-site", tenant_id=VISITOR_TENANT, user_id=VISITOR_ID, data=profile, created_at=at)
    db.add(site)
    for sid, title, resolved in [("visitor-example-502", "示例：结算接口返回 502", False),
                                  ("visitor-example-pool", "示例：连接等待超时后的处理记录", True)]:
        db.add(ChatSession(id=sid, tenant_id=VISITOR_TENANT, user_id=VISITOR_ID, title=title, created_at=at))
        db.flush()
        text = ("2026-11-01T10:00:00+08:00 GET /orders HTTP 502\n"
                "2026-11-01T10:00:01+08:00 connect() failed (111: Connection refused) while connecting to upstream")
        summary = "教学样例中，结算请求返回502且上游连接被拒绝；仍需核对应用进程和同期日志。"
        report = {"workflow": "diagnose", "summary": summary,
                  "observations": [{"evidence_id": "E1", "line_start": 1, "line_end": 2,
                                    "meaning": "样例记录了请求失败和上游拒绝连接，不足以确定应用退出原因。", "quote": text}],
                  "hypotheses": ["上游应用可能未启动，或监听地址与代理配置不一致"],
                  "missing_info": ["上游应用同一时间的日志与实际监听地址"], "references": [],
                  "steps": [{"location": "应用服务器", "action": "docker compose ps\ndocker compose logs --tail 100 app",
                             "normal": "服务进程存在且监听地址与Nginx配置一致", "abnormal": "保存退出原因并核对端口",
                             "risk": "只读检查", "rollback": "只读检查无需回退"}],
                  "verification": {"verdict": "inconclusive", "evidence_ids": ["E1"], "reason": "此为教学文本，不证明任何真实网站状态。"}}
        if resolved:
            text = "2026-11-01T10:00:00+08:00 GET /orders HTTP 500\nsqlalchemy.exc.TimeoutError: QueuePool limit reached"
            summary = "教学案例：确认连接未及时释放，修正应用连接使用方式后，用户核对请求并确认解决。"
            report = {**report, "summary": summary, "observations": [{"evidence_id": "E1", "line_start": 2, "line_end": 2,
                       "quote": text.splitlines()[1], "meaning": "样例包含连接池等待超时；真实环境仍需结合连接使用情况分析。"}],
                      "hypotheses": [], "missing_info": []}
        data = {"kind": "problem", "profile": profile, "problem": {"symptom": title, "endpoint": "/orders"},
                "evidence": [{"id": "E1", "label": "教学样例日志", "kind": "initial", "text": text,
                              "collected_at": "2026-11-01T10:00:00+08:00", "submitted_at": at.isoformat()}],
                "report": report, "report_hits": [], "revision": 1, "archived": False,
                "solution": "教学案例已由示例用户确认解决；不是平台实测结论。" if resolved else ""}
        db.add(SupportCase(session_id=sid, tenant_id=VISITOR_TENANT, user_id=VISITOR_ID,
                           site_id="visitor-example-site", status="user_resolved" if resolved else "investigating",
                           severity="P2" if resolved else "P1", labels=["教学示例"], data=data, created_at=at, updated_at=at))
        db.add_all([Message(session_id=sid, tenant_id=VISITOR_TENANT, role="user", content=title, created_at=at),
                    Message(session_id=sid, tenant_id=VISITOR_TENANT, role="assistant", content=summary, created_at=at)])
        record_event(db, VISITOR_TENANT, VISITOR_ID, "case", sid, "case_created",
                     {"title": title, "actor": "教学示例", "note": "预置教学记录，非真实采集。"})
    ensure_visitor_context(db, site)
    db.commit()
