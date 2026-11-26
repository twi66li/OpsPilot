"""可回放的故障演练。观测、审批与复测均为预置样本。"""

from typing import NamedTuple

from app.db.models import AgentRun, RemediationRequest
from app.services.official_library import BY_ID
from app.services.visitor_examples import Turn
from app.services.visitor_history import save_demo_session, stamp
from app.services.visitor_service import DEMO_DRILL_PREFIX, VISITOR_TENANT


class Event(NamedTuple):
    at: str
    name: str
    summary: str
    result: dict


class Drill(NamedTuple):
    key: str
    title: str
    source: str
    turns: tuple[Turn, ...]
    events: tuple[Event, ...]


PROBES = [
    {"at": "2026-11-23T16:02:17+08:00", "status": 200, "elapsed_ms": 76},
    {"at": "2026-11-23T16:02:28+08:00", "status": 200, "elapsed_ms": 81},
    {"at": "2026-11-23T16:02:43+08:00", "status": 200, "elapsed_ms": 69},
]

DRILLS = (
    Drill(
        "upstream", "演练：从 502 定位上游监听", "official-nginx-upstream",
        (
            Turn("08-11 10:03", "08-11 10:05", "网关在刚发布后连续报 502。请先把连接问题和上游应用报错分开。",
                 "17 个失败请求都记录了 connection refused，同一批请求在应用侧没有入口日志。下一步从网关所在网络核对监听地址。"),
            Turn("08-11 10:14", "08-11 10:16", "发现演练代理还指向 8000，服务这次启动在 8080。该延长 proxy_read_timeout 吗？",
                 "拒绝连接发生在读取响应之前，延长等待解决不了监听地址不匹配。把旧配置和新监听写进变更申请，先核对配置再复测。"),
            Turn("08-11 10:48", "08-11 10:50", "值班同学核对配置后完成测试切换，原请求三次通过。新日志也没有拒绝连接。",
                 "样本确认新监听路径可用。保留三个请求编号和变更前配置，发布检查补上网关到上游的连通性。"),
        ),
        (
            Event("08-11 10:04", "query_logs", "同一发布窗口集中出现 17 次上游连接拒绝。",
                  {"http_status": 502, "count": 17, "error": "connect() failed (111: Connection refused)"}),
            Event("08-11 10:13", "route_intent", "对照代理目标和演练进程实际监听。",
                  {"configured_port": 8000, "observed_port": 8080}),
            Event("08-11 10:46", "query_service_metrics", "旧失败请求路径复跑三次。",
                  {"http_statuses": [200, 200, 200], "window": "10:46–10:48"}),
        ),
    ),
    Drill(
        "capacity", "演练：连接等待上涨，暂缓扩池", "official-postgres-connections",
        (
            Turn("09-09 14:31", "09-09 14:33", "压测时等待变长，上限是 80，要不要先把应用池加倍？",
                 "先保存应用进程数、连接池等待、数据库活跃连接与长事务数。申请调整前要核对所有进程的连接需求总和。"),
            Turn("09-09 14:48", "09-09 14:52", "有 4 个应用进程，压测窗口 active=54，idle in transaction=8。扩大池的申请先等等？",
                 "上限有管理和其他服务的预留。先检查 8 个空闲事务的持有时间与关联请求，现有样本不足以批准扩池。"),
            Turn("09-10 09:08", "09-10 09:11", "值班同事没有批准扩池。复测想先查异常路径退出后能不能归还连接。",
                 "把申请退回理由、事务持有时间和同并发前后样本写入复盘。保持压测场景，等下一轮证据再关闭记录。"),
        ),
        (
            Event("09-09 14:32", "query_service_metrics", "取同负载下的等待分布与数据库活动。",
                  {"pool_wait_p95_ms": 514, "active": 54, "idle_in_transaction": 8, "max_connections": 80}),
            Event("09-09 14:56", "approval_gate", "扩池提议被退回，改成核对长事务和归还情况。",
                  {"decision": "rejected", "reviewer": "唐哲", "action_executed": False}),
            Event("09-10 09:10", "generate", "列出同并发复测需要的观测项；故障原因待核对。", {"status": "needs_information"}),
        ),
    ),
    Drill(
        "release-window", "演练：区分发布影响与流量波动", "official-nginx-upstream",
        (
            Turn("10-12 20:06", "10-12 20:07", "20:00 上新后 P95 增长，和发布有关吗？报警只有两个采样点。",
                 "把发布节点、请求量、失败数和同路径延迟对到同一窗口。先看有无版本分组或未升级节点可作为对照。"),
            Turn("10-12 20:27", "10-12 20:29", "旧版和新版的读接口都变慢；订单写接口没抖，读流量比昨天同时间多 2.1 倍。",
                 "目前更值得检查读流量相关资源。保留分版本明细和峰值窗口，暂不发起回滚。"),
            Turn("10-13 10:41", "10-13 10:43", "次日用了相近读流量重放，也看到缓存未命中升高。P95 回到 240ms，能记为发布没影响吗？",
                 "只能记录未观察到分版本差异，以及该样本内的缓存关系。保留其他变更与峰值样本，避免替所有场景作结论。"),
        ),
        (
            Event("10-12 20:07", "query_service_metrics", "读流量同比增加，写路径延迟稳定。",
                  {"read_traffic_ratio": 2.1, "write_p95_ms": 108}),
            Event("10-12 20:28", "query_fault_history", "新旧版本在相同时间窗内均出现读请求变慢。",
                  {"release_at": "20:00", "older_p95_ms": 782, "newer_p95_ms": 805}),
            Event("10-13 10:42", "generate", "保存回放对照，暂无证据要求回滚。",
                  {"matched_load_p95_ms": 240, "action_executed": False}),
        ),
    ),
    Drill(
        "pool-recovery", "演练：连接池超时与三次复测", "official-sqlalchemy-pool",
        (
            Turn("11-23 15:42", "11-23 15:43:26",
                 "order-service 在 15:37 后开始出现 500。查过去五分钟的请求、日志和连接等待，把实测与演练设定分开。",
                 "同一窗口 27 笔请求有 9 笔 500，P95 约 1.19 秒。应用日志出现 ERR_5003；先核对演练注入状态，再判断是否需要看真实连接占用。"),
            Turn("11-23 15:49", "11-23 15:51",
                 "演练控制台显示连接池超时注入已打开。怎么设计关闭前后的验证？",
                 "保存当前失败样本和注入状态。仅申请关闭 order-service 的演练注入，经批准后从同一入口间隔发送三次请求，记录状态码和耗时。"),
            Turn("11-23 16:03", "11-23 16:05",
                 "周宁批准后关闭了注入。16:02 的三笔都返回 200，耗时 76、81、69ms。下一步怎么记？",
                 "三次同入口复测通过，样本内的 ERR_5003 不再复现。记录关闭前的 9/27 失败率、审批和三笔响应，仅对这次演练作结。"),
        ),
        (
            Event("11-23 15:42:39", "query_service_metrics", "提取异常窗口的请求数与延迟。",
                  {"requests": 27, "failed_5xx": 9, "p95_ms": 1190, "window": "15:37–15:42"}),
            Event("11-23 15:43:02", "query_logs", "应用日志记录 ERR_5003，出现在测试注入窗口。",
                  {"code": "ERR_5003", "service": "order-service", "error": "QueuePool wait timeout"}),
            Event("11-23 15:43:18", "search_runbook", "引用连接池等待检查手册，区分异常现象与根因。", {}),
            Event("11-23 15:56", "approval_gate", "周宁批准关闭测试注入，范围为 order-service。",
                  {"decision": "approved", "reviewer": "周宁", "scope": "order-service 演练实例"}),
            Event("11-23 16:01", "remediation", "演练记录显示固定恢复动作完成，注入关闭。",
                  {"action": "reset_demo_fault", "injection_enabled": False}),
            Event("11-23 16:02:43", "query_service_metrics", "相同入口三次请求均返回 200。", {"probes": PROBES}),
        ),
    ),
)


def ensure_visitor_drills(db):
    for drill in DRILLS:
        session_id = DEMO_DRILL_PREFIX + drill.key
        run_id = session_id + "-run"
        if db.get(AgentRun, run_id):
            continue
        save_demo_session(db, session_id, drill.title, drill.turns, run_id=run_id)
        handbook = BY_ID[drill.source]
        hit = {"chunk_id": "demo-" + drill.source, "doc_id": drill.source,
               "doc_title": handbook["title"], "source": handbook["url"], "source_kind": "official",
               "applicable_version": handbook["applicable_version"], "checked_at": handbook["checked_at"],
               "text": handbook["content"].splitlines()[0], "parent_text": handbook["content"], "citation_id": 1}
        result = {"end_reason": "answered", "answer": drill.turns[-1].answer,
                  "message": "记录已归档 · 演练回放", "hits": [hit]}
        data = {"question": drill.turns[0].question, "result": result, "demo": True, "events": [
            {"kind": "tool", "at": stamp(event.at).isoformat(),
             "detail": {"type": "tool", "name": event.name, "summary": event.summary, "result": event.result}}
            for event in drill.events
        ]}
        if drill.key == "pool-recovery":
            operation_id = session_id + "-recovery"
            data["demo_operation_id"] = operation_id
            db.add(RemediationRequest(id=operation_id, tenant_id=VISITOR_TENANT, run_id=run_id,
                                      action="reset_demo_fault", service="order-service", requested_by="林悦",
                                      reason=drill.turns[1].question, status="succeeded", approved=True, decided_by="周宁",
                                      comment="仅关闭 order-service 的演练注入，需在同一入口复测三次。",
                                      result={"before": {"requests": 27, "failed": 9, "p95_ms": 1190}, "after": PROBES},
                                      created_at=stamp("11-23 15:52"), decided_at=stamp("11-23 15:56")))
        db.add(AgentRun(id=run_id, tenant_id=VISITOR_TENANT, session_id=session_id, mode="demo",
                        status="completed", data=data, created_at=stamp(drill.turns[0].asked_at),
                        updated_at=stamp(drill.turns[-1].answered_at)))
