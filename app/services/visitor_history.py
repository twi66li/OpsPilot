"""保存公开演示的合成记录，已有部署按稳定 ID 更新。"""

import hashlib
from datetime import datetime, timedelta

from app.db.models import (
    ApprovalRequest,
    ChatSession,
    Document,
    DocumentChunk,
    DocumentParent,
    DocumentVersion,
    Message,
    SupportCase,
    Tenant,
    Ticket,
    WebsiteProfile,
    WorkspaceEvent,
)
from app.services.knowledge_access import personal_namespace
from app.services.visitor_examples import INCIDENTS, Turn
from app.services.visitor_service import DEMO_RECORD_PREFIX, VISITOR_ID, VISITOR_TENANT

HISTORY_VERSION = 3


def stamp(value: str) -> datetime:
    return datetime.fromisoformat(f"2026-{value}") - timedelta(hours=8)


def save_demo_session(db, session_id, title, turns, *, assistant_chat=False, run_id=None):
    session = db.get(ChatSession, session_id)
    if session is not None:
        if (session.tenant_id, session.user_id) != (VISITOR_TENANT, VISITOR_ID):
            raise RuntimeError("演示会话 ID 已被其他账号使用")
        session.title = title
        session.created_at = stamp(turns[0].asked_at)
    else:
        db.add(ChatSession(id=session_id, tenant_id=VISITOR_TENANT, user_id=VISITOR_ID, title=title,
                           assistant_chat=assistant_chat, created_at=stamp(turns[0].asked_at)))
        db.flush()
    messages = db.query(Message).filter_by(session_id=session_id, tenant_id=VISITOR_TENANT).order_by(Message.id).all()
    entries = [(role, content, at) for turn in turns for role, content, at in (
        ("user", turn.question, turn.asked_at), ("assistant", turn.answer, turn.answered_at))]
    for position, (role, content, at) in enumerate(entries):
        if position < len(messages):
            message = messages[position]
        else:
            message = Message(session_id=session_id, tenant_id=VISITOR_TENANT)
            db.add(message)
        message.role, message.content, message.created_at = role, content, stamp(at)
        message.meta = {"run_id": run_id} if run_id and position == len(entries) - 1 else {}


def case_event(db, case_id, title, at):
    existing = db.query(WorkspaceEvent).filter_by(
        tenant_id=VISITOR_TENANT, user_id=VISITOR_ID, resource_type="case", resource_id=case_id, kind="case_created",
    ).first()
    if existing:
        existing.created_at = at
    else:
        db.add(WorkspaceEvent(tenant_id=VISITOR_TENANT, user_id=VISITOR_ID, resource_type="case", resource_id=case_id,
                              kind="case_created", detail={"title": title}, created_at=at))


def _upgrade_legacy_examples(db):
    examples = (
        ("visitor-example-502", "示例：结算接口返回 502", (
            Turn("11-01 10:04", "11-01 10:05", "结算服务发布 v2 后，两笔下单都返回了 502，先从哪看？",
                 "在同一请求时间点对照 Nginx 访问与错误日志。记录网关是否连接过上游，再看应用端。"),
            Turn("11-01 10:16", "11-01 10:17", "网关记录 connect() failed (111: Connection refused)，应用没有对应请求记录。",
                 "证据落在连接阶段。先对照容器是否在线及服务实际监听的端口；暂不改代理超时。"),
            Turn("11-01 11:48", "11-01 11:50", "拿到发布记录了，应用容器是否重启还要值班同事确认。我先不动配置。",
                 "保持排查中，补上 09:55–10:10 容器状态和退出原因，再与发布记录对齐。"),
        )),
        ("visitor-example-pool", "示例：连接等待超时后的处理记录", (
            Turn("08-21 16:53", "08-21 16:54", "订单接口两次 500，日志只有 QueuePool limit reached，需要增加池吗？",
                 "先确认异常请求结束后连接是否归还。暂时把进程数、池容量和使用量按时间对齐。"),
            Turn("08-21 17:12", "08-21 17:13", "错误分支有个 Session 没走关闭逻辑，压测窗口结束后连接还没回落。",
                 "修正错误分支的会话生命周期。复测同一批请求，核对结束后占用能否回到起始值。"),
            Turn("08-22 11:26", "08-22 11:28", "同等并发重跑 30 笔都返回 200，结束后三分钟连接从 22 回到 5。",
                 "复测覆盖了原来失败的路径，连接占用也回到起始范围。这条问题可由维护者确认解决。"),
        )),
    )
    for session_id, title, turns in examples:
        case = db.get(SupportCase, session_id)
        if not case or (case.tenant_id, case.user_id) != (VISITOR_TENANT, VISITOR_ID):
            continue
        if case.data.get("demo_history_version") == HISTORY_VERSION:
            continue
        save_demo_session(db, session_id, title, turns)
        case.created_at, case.updated_at = stamp(turns[0].asked_at), stamp(turns[-1].answered_at)
        evidence = dict(case.data["evidence"][0])
        if session_id == "visitor-example-pool":
            evidence["text"] = ("2026-08-21T16:53:02+08:00 GET /orders HTTP 500\n"
                                "sqlalchemy.exc.TimeoutError: QueuePool limit reached")
            evidence["collected_at"] = "2026-08-21T16:53:02+08:00"
            evidence["submitted_at"] = stamp(turns[1].asked_at).isoformat()
        report = {**case.data["report"], "summary": turns[-1].answer}
        if session_id == "visitor-example-pool":
            report["observations"] = [{**report["observations"][0], "quote": evidence["text"].splitlines()[1]}]
        case.data = {**case.data, "report": report, "evidence": [evidence], "demo_history_version": HISTORY_VERSION}
        case_event(db, session_id, title, case.created_at)


DEMO_DOCS = (
    (
        "visitor-demo-doc-nginx",
        "商城网关与上游发布清单 v2",
        "mall-nginx-upstream-v2.md",
        "08-05 14:20",
        "## 1. 上游端口与健康检查\n"
        "- 主站 API 容器监听 `8000`，宿主机回环映射 `127.0.0.1:18000`，Nginx 统一代理 `/api/` 与前端静态页。\n"
        "- 发布前先执行 `curl -fsS http://127.0.0.1:18000/ready` 确认数据库、Milvus 与缓存全绿，再切流量。\n\n"
        "## 2. 502 / 504 快速排查顺序\n"
        "- 若 Nginx error.log 报 `111: Connection refused`，优先核对容器在线状态与监听端口，不盲目拉大 `proxy_read_timeout`。\n"
        "- SSE 接口必须保留 `proxy_buffering off` 与 `proxy_read_timeout 300s`。\n\n"
        "## 3. 回滚与复测核对\n"
        "- 回滚后对 `/orders` 与 `/ready` 连续发起 3 次只读探针，确认状态码 200 且耗时回落至 120 ms 内。",
    ),
    (
        "visitor-demo-doc-postgres",
        "PostgreSQL 连接池与慢查询巡检手册",
        "postgres-pool-runbook.md",
        "09-12 11:10",
        "## 1. 连接池配额基线\n"
        "- 本地/演示库 `max_connections=30`，单副本业务池 `5+3=8` + LangGraph 检查点池 `4`，合计不超过 12，给管理连接预留 6。\n"
        "- 遇到 `QueuePool limit reached` 时，先查异常分支是否未归还 Session，再核对慢 SQL。\n\n"
        "## 2. 活动会话与长事务检查\n"
        "- 使用只读 SQL 检查 `pg_stat_activity` 中 `idle in transaction` 数量与持续时间。\n"
        "- 未定位泄漏前禁止直接加倍连接池上限，防止耗尽数据库总连接。\n\n"
        "## 3. 恢复验收标准\n"
        "- 同并发复测 30 笔请求全部 200，压测结束后 3 分钟内活跃连接回落到基线水位。",
    ),
    (
        "visitor-demo-doc-export",
        "报表导出与对账任务值班 SOP",
        "billing-export-sop.md",
        "10-18 16:40",
        "## 1. 大报表队列隔离原则\n"
        "- 导出超过 5 万行的账单任务走独立异步队列，避免占用短请求 worker 导致接口整体排队。\n"
        "- 监控按文件尺寸分组记录 P95，不能用小报表样本代替全队列结论。\n\n"
        "## 2. 外部对账接口限流处理\n"
        "- 渠道返回 HTTP 429 时严格遵循响应头 `Retry-After` 退避，禁止高频重试放大错误率。\n\n"
        "## 3. 值班交接清单\n"
        "- 每次处理完导出积压后，将异常窗口日志、审批记录和 3 次复测结果归档至对应问题记录。",
    ),
)


def _probe_series(points: tuple[tuple[str, int, int, str], ...]) -> list[dict]:
    return [
        {
            "collected_at": f"2026-{at}+08:00",
            "elapsed_ms": ms,
            "http_status": code,
            "state": state,
            "timed_out": False,
            "source_kind": "read_only_http",
        }
        for at, code, ms, state in points
    ]


PRIMARY_PROBES = _probe_series((
    ("11-20T09:00:00", 200, 84, "normal"),
    ("11-20T15:00:00", 200, 91, "normal"),
    ("11-21T09:00:00", 200, 79, "normal"),
    ("11-21T18:00:00", 200, 88, "normal"),
    ("11-22T10:00:00", 200, 82, "normal"),
    ("11-22T20:00:00", 200, 96, "normal"),
    ("11-23T10:00:00", 200, 86, "normal"),
    ("11-23T15:40:00", 502, 1180, "abnormal"),
    ("11-23T15:52:00", 500, 940, "abnormal"),
    ("11-23T16:03:00", 200, 76, "normal"),
    ("11-24T09:00:00", 200, 81, "normal"),
    ("11-24T18:00:00", 200, 85, "normal"),
    ("11-25T09:00:00", 200, 78, "normal"),
    ("11-25T18:00:00", 200, 83, "normal"),
    ("11-26T09:00:00", 200, 74, "normal"),
    ("11-26T15:00:00", 200, 79, "normal"),
))

BILLING_PROBES = _probe_series((
    ("11-21T09:00:00", 200, 112, "normal"),
    ("11-22T09:00:00", 200, 108, "normal"),
    ("11-22T21:00:00", 200, 134, "normal"),
    ("11-23T09:00:00", 200, 118, "normal"),
    ("11-24T09:00:00", 200, 105, "normal"),
    ("11-24T21:00:00", 200, 129, "normal"),
    ("11-25T09:00:00", 200, 102, "normal"),
    ("11-25T21:00:00", 200, 115, "normal"),
    ("11-26T09:00:00", 200, 98, "normal"),
    ("11-26T15:00:00", 200, 104, "normal"),
))

STAGING_PROBES = _probe_series((
    ("11-22T14:00:00", 200, 68, "normal"),
    ("11-23T14:00:00", 200, 72, "normal"),
    ("11-24T14:00:00", 200, 65, "normal"),
    ("11-25T14:00:00", 200, 71, "normal"),
    ("11-26T10:00:00", 200, 64, "normal"),
    ("11-26T14:30:00", 200, 69, "normal"),
))

SITE_CHANGES = (
    ("visitor-example-site", "08-22 10:15", "release", "修复订单错误分支连接回收 v1.8.2", "v1.8.2", "周宁",
     "修复异常路径未归还 SQLAlchemy Session 的问题，复测连接回落正常。"),
    ("visitor-example-site", "09-18 19:30", "config", "收紧 Nginx 上游连接与 SSE 读超时", "nginx-1.24", "唐哲",
     "为流式响应开启 proxy_buffering off 并记录上游连接拒绝指标。"),
    ("visitor-example-site", "10-12 20:00", "release", "发布商城商品详情缓存优化 v1.9.4", "v1.9.4", "周宁",
     "增加版本指纹缓存 Key，降低高峰期读接口 P95 抖动。"),
    ("visitor-demo-site-billing", "09-26 16:00", "config", "渠道对账接入 Retry-After 指数退避", "v1.4.1", "林岚",
     "收到 429 后按供应商响应头延迟重试，消除整点批次失败。"),
    ("visitor-demo-site-billing", "10-25 14:10", "release", "大报表导出迁入独立异步队列 v1.5.0", "v1.5.0", "林岚",
     "隔离 5 万行以上导出任务与常规查询，按文件尺寸分组统计耗时。"),
    ("visitor-demo-site-staging", "11-19 11:20", "config", "预发环境验证 PostgreSQL 30 连接池配额", "pg16-pool", "唐哲",
     "按单副本 12、双副本 24 连接核算并完成压测留痕。"),
)


def _ensure_visitor_docs(db) -> None:
    ns = personal_namespace(VISITOR_ID)
    if not db.get(Tenant, ns):
        db.add(Tenant(id=ns, name="公开演示私有资料"))
        db.flush()
    for doc_id, title, filename, created_str, content in DEMO_DOCS:
        ver_id = doc_id + "-v1"
        at = stamp(created_str)
        sections = [part.strip() for part in content.split("\n\n") if part.strip()]
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        doc = db.get(Document, doc_id)
        if doc is None:
            db.add(
                Document(
                    id=doc_id,
                    tenant_id=ns,
                    title=title,
                    source=f"demo/{filename}",
                    content=content,
                    chunk_count=len(sections),
                    active_version_id=ver_id,
                    created_by=VISITOR_ID,
                    created_at=at,
                )
            )
            db.flush()
        elif doc.active_version_id != ver_id or doc.chunk_count != len(sections):
            doc.title, doc.content, doc.chunk_count, doc.active_version_id = title, content, len(sections), ver_id

        ver = db.get(DocumentVersion, ver_id)
        pages = [{"page": 1, "text": content}]
        if ver is None:
            db.add(
                DocumentVersion(
                    id=ver_id,
                    tenant_id=ns,
                    doc_id=doc_id,
                    number=1,
                    filename=filename,
                    content_hash=digest,
                    original_path="",
                    parser="markdown",
                    status="ready",
                    progress=100,
                    pages=pages,
                    warnings=["公开演示预置手册，仅用于只读浏览与排查对照。"],
                    index_config={"demo": True},
                    created_at=at,
                    updated_at=at,
                )
            )
            db.flush()

        for idx, section in enumerate(sections):
            parent_id = f"{ver_id}:p{idx}"
            chunk_id = f"{ver_id}:c{idx}"
            heading = section.splitlines()[0].lstrip("# ").strip()
            location = {"heading_path": [heading], "page_start": 1, "page_end": 1}
            if db.get(DocumentParent, parent_id) is None:
                db.add(
                    DocumentParent(
                        id=parent_id,
                        tenant_id=ns,
                        doc_id=doc_id,
                        version_id=ver_id,
                        text=section,
                        location=location,
                    )
                )
            if db.get(DocumentChunk, chunk_id) is None:
                db.add(
                    DocumentChunk(
                        id=chunk_id,
                        tenant_id=ns,
                        doc_id=doc_id,
                        version_id=ver_id,
                        parent_id=parent_id,
                        payload={
                            "chunk_id": chunk_id,
                            "doc_id": doc_id,
                            "doc_title": title,
                            "source": f"demo/{filename}",
                            "source_name": filename,
                            "text": section,
                            "chunk_index": idx,
                            "version_id": ver_id,
                            "version": 1,
                            "parent_id": parent_id,
                            "content_type": "text",
                            "parser": "markdown",
                            **location,
                        },
                    )
                )


def _ensure_visitor_sites_and_events(db, primary_site) -> None:
    # 1. 升级主演示站点：补全环境、负责人、绑定手册与连续巡检趋势点
    primary_site.data = {
        **primary_site.data,
        "environment": "production",
        "group": "核心交易",
        "owner": "周宁",
        "doc_ids": ["visitor-demo-doc-nginx", "visitor-demo-doc-postgres"],
        "health_check": PRIMARY_PROBES[-1],
        "probe_history": PRIMARY_PROBES,
        "demo_history_version": HISTORY_VERSION,
    }
    # 2. 补充两个跨环境演示站点，呈现多业务长期维护状态
    extra_sites = (
        (
            "visitor-demo-site-billing",
            "08-10 09:30",
            {
                "name": "账单与对账后台（示例）",
                "url": "",
                "environment": "production",
                "group": "财务与报表",
                "owner": "林岚",
                "framework": "FastAPI + Celery",
                "deployment": "Docker Compose",
                "database": "PostgreSQL 16 + Redis 7",
                "versions": "Python 3.12",
                "doc_ids": ["visitor-demo-doc-postgres", "visitor-demo-doc-export"],
                "dependencies": [
                    {"source": "Nginx", "target": "billing-api", "note": "财务后台入口"},
                    {"source": "billing-api", "target": "PostgreSQL", "note": "账单明细与对账状态"},
                    {"source": "export-worker", "target": "Redis", "note": "异步大报表导出队列"},
                ],
                "notes": "负责每日渠道对账与异步大报表导出，演示多站点隔离维护记录。",
                "health_check": BILLING_PROBES[-1],
                "probe_history": BILLING_PROBES,
                "demo_history_version": HISTORY_VERSION,
            },
        ),
        (
            "visitor-demo-site-staging",
            "09-02 15:00",
            {
                "name": "预发验证环境（示例）",
                "url": "",
                "environment": "staging",
                "group": "核心交易",
                "owner": "唐哲",
                "framework": "FastAPI + Nginx",
                "deployment": "Docker Compose（单机 2C2G）",
                "database": "PostgreSQL 16 + Milvus 2.4.15",
                "versions": "Python 3.12",
                "doc_ids": ["visitor-demo-doc-nginx"],
                "dependencies": [
                    {"source": "Nginx", "target": "staging-api", "note": "预发验收流量"},
                    {"source": "staging-api", "target": "Milvus", "note": "检索回归核对"},
                ],
                "notes": "发版前验证配置变更、连接池配额与回归评测的预发环境。",
                "health_check": STAGING_PROBES[-1],
                "probe_history": STAGING_PROBES,
                "demo_history_version": HISTORY_VERSION,
            },
        ),
    )
    for site_id, created_str, data in extra_sites:
        existing = db.get(WebsiteProfile, site_id)
        if existing is None:
            db.add(
                WebsiteProfile(
                    id=site_id,
                    tenant_id=VISITOR_TENANT,
                    user_id=VISITOR_ID,
                    data=data,
                    created_at=stamp(created_str),
                )
            )
        elif existing.data.get("demo_history_version") != HISTORY_VERSION:
            existing.data = data

    # 3. 补齐跨月站点变更时间线（按 title 幂等）
    existing_titles = {
        (ev.resource_id, (ev.detail or {}).get("title"))
        for ev in db.query(WorkspaceEvent)
        .filter_by(tenant_id=VISITOR_TENANT, user_id=VISITOR_ID, resource_type="site", kind="site_change")
        .all()
    }
    for site_id, at_str, change_kind, title, version, actor, note in SITE_CHANGES:
        if (site_id, title) in existing_titles:
            continue
        at = stamp(at_str)
        db.add(
            WorkspaceEvent(
                tenant_id=VISITOR_TENANT,
                user_id=VISITOR_ID,
                resource_type="site",
                resource_id=site_id,
                kind="site_change",
                detail={
                    "change_kind": change_kind,
                    "title": title,
                    "version": version,
                    "occurred_at": at.isoformat() + "+00:00",
                    "actor": actor,
                    "note": note,
                    "source": "预置演示历史",
                },
                created_at=at,
            )
        )


def ensure_visitor_history(db, site):
    tenant, owner = VISITOR_TENANT, VISITOR_ID
    site.created_at = min(site.created_at, stamp("08-03 10:00"))
    _ensure_visitor_docs(db)
    _ensure_visitor_sites_and_events(db, site)
    _upgrade_legacy_examples(db)
    for example in INCIDENTS:
        root = DEMO_RECORD_PREFIX + example.key
        case_id, chat_id, approval_id = root + "-case", root + "-chat", root + "-approval"
        previous = db.get(SupportCase, case_id)
        if previous and previous.data.get("demo_history_version") == HISTORY_VERSION:
            continue
        title = "示例：" + example.title
        save_demo_session(db, case_id, title, example.turns)
        save_demo_session(db, chat_id, example.title, example.turns, assistant_chat=True)
        at, updated_at = stamp(example.turns[0].asked_at), stamp(example.turns[-1].answered_at)
        initial_at = stamp(example.turns[1].asked_at).isoformat()
        evidence = [{"id": "E1", "label": "异常窗口", "kind": "initial", "text": example.evidence,
                     "collected_at": initial_at + "Z", "submitted_at": initial_at}]
        observations = [{"evidence_id": "E1", "line_start": 1, "line_end": len(example.evidence.splitlines()),
                         "quote": example.evidence, "meaning": example.turns[1].answer}]
        if example.verification:
            checked_at = stamp(example.turns[-1].asked_at).isoformat()
            evidence.append({"id": "E2", "label": "处理后复测", "kind": "check_result", "text": example.verification,
                             "collected_at": checked_at + "Z", "submitted_at": checked_at})
            observations.append({"evidence_id": "E2", "line_start": 1, "line_end": len(example.verification.splitlines()),
                                 "quote": example.verification, "meaning": example.turns[-1].answer})
        outcome = example.turns[-1].answer
        report = {"workflow": "diagnose", "summary": outcome, "observations": observations,
                  "hypotheses": [], "missing_info": [], "references": [], "steps": [],
                  "verification": {"verdict": "sample_recovered" if example.verification else "inconclusive",
                                   "evidence_ids": [e["id"] for e in evidence], "reason": outcome}}
        db.merge(SupportCase(session_id=case_id, tenant_id=tenant, user_id=owner, site_id=site.id,
                             status="user_resolved" if example.verification else "investigating", severity="P2",
                             labels=["演示历史"], created_at=at, updated_at=updated_at,
                             data={"kind": "problem", "profile": site.data, "revision": 1, "archived": False,
                                   "problem": {"symptom": example.title, "endpoint": example.endpoint},
                                   "report": report, "report_hits": [], "evidence": evidence,
                                   "solution": outcome if example.verification else "",
                                   "demo_history_version": HISTORY_VERSION}))
        case_event(db, case_id, title, at)
        review = example.review
        decided = stamp(review.decided_at) if review.decided_at else None
        db.merge(ApprovalRequest(id=approval_id, tenant_id=tenant, session_id=case_id, requested_by=review.requester,
                                 ticket_draft={"title": example.title + "跟进", "description": example.follow_up,
                                               "severity": "P2"}, status=review.status, created_at=stamp(review.requested_at),
                                 decided_at=decided, decided_by=review.reviewer, comment=review.comment))
        if review.status == "approved":
            db.merge(Ticket(id=root + "-ticket", tenant_id=tenant, session_id=case_id, approval_id=approval_id,
                            title=example.title + "跟进", description=example.follow_up + "\n进展：" + outcome,
                            severity="P2", status="resolved" if example.verification else "open",
                            created_by=review.requester, approver=review.reviewer, decision_comment=review.comment,
                            created_at=decided, decided_at=decided))


# 后续批次保留前期基准题，新增当月已出现的故障场景。失败回答保留实际遗漏点。
EVAL_BATCHES = {
    8: ("08-29 16:24", 3, {"stock": "建议先重启应用，再观察库存接口。遗漏了同步批次与外部读取耗时的对照。"}),
    9: ("09-28 10:07", 6, {"report": "建议降低客户端并发，但没有识别 Retry-After 或验证下一次整点批次。"}),
    10: ("10-31 19:46", 9, {"search": "建议增加连接池容量，忽略了其他路由正常且连接等待只有 12ms。"}),
    11: ("11-26 14:38", 12, {"export": "识别了大文件占位，但把单个小报表样本外推成全队列结论，漏掉尺寸分组 P95。"}),
}

DEMO_RUNS = [
    {"id": f"visitor-eval-2026-{month:02}", "label": f"2026 年 {month} 月 · 诊断回归（演示）",
     "category": "evaluation", "question_count": batch[1]}
    for month, batch in reversed(tuple(EVAL_BATCHES.items()))
]


def visitor_eval_report(run):
    if not any(meta["id"] == run for meta in DEMO_RUNS):
        return None
    at, count, failures = EVAL_BATCHES[int(run[-2:])]
    questions = []
    for example in INCIDENTS[:count]:
        failure = failures.get(example.key)
        questions.append({"id": example.key, "question": example.turns[0].question,
                          "answer": failure or example.turns[1].answer, "status": "failed" if failure else "passed",
                          "evidence": [example.evidence], "program_checks": {"uses_observed_evidence": not failure},
                          "usage": {}})
    return {"id": run, "summary": {"口径": "合成演示样本", "评测时间（北京时间）": f"2026-{at}",
                                   "通过": count - len(failures), "样本": count, "需复核": list(failures)},
            "questions": questions}
