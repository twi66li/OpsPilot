"""种子数据脚本：python -m scripts.seed

创建租户/用户/知识库文档/历史故障记录，并对文档执行入库（分块 → 向量化 → Qdrant/BM25）。
幂等：重复执行会跳过已存在的数据。
"""
import argparse
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.auth.security import hash_password
from app.config import get_settings
from app.container import get_container
from app.db.base import SessionLocal, init_db
from app.db.models import Document, FaultRecord, Tenant, User
from app.services import kb_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("seed")

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"

# (文件名, 知识库标题)
KNOWLEDGE_DOCS = [
    ("ERR_5003_数据库连接池耗尽处理手册.md", "ERR_5003 数据库连接池耗尽处理手册"),
    ("order-service_接口超时排查SOP.md", "order-service 接口超时排查 SOP"),
    ("payment-service_对账失败处理手册.md", "payment-service 对账失败处理手册"),
    ("Redis集群主从切换SOP.md", "Redis 集群主从切换 SOP"),
    ("Pod_OOMKilled处理指南.md", "Pod OOMKilled 处理指南"),
    ("网关502排查手册.md", "网关 502 排查手册"),
    ("MySQL慢查询治理.md", "MySQL 慢查询治理"),
    ("Kafka消费积压处理.md", "Kafka 消费积压处理"),
    ("服务熔断与限流说明.md", "服务熔断与限流说明"),
    ("日志平台使用指南.md", "日志平台使用指南"),
    ("磁盘空间不足处理SOP.md", "磁盘空间不足处理 SOP"),
]

USERS = [
    # (tenant, username, password, role)
    ("default", "admin", "admin123", "admin"),
    ("default", "dev01", "dev123456", "engineer"),
    ("default", "viewer01", "view123456", "viewer"),
    ("acme", "acme_admin", "acme123456", "admin"),
]

FAULT_RECORDS = [
    ("order-service", "ERR_5003", "订单服务连接池耗尽导致大面积超时", "下单接口 RT 从 80ms 涨到 8s，应用日志 ERR_5003", "大促预热流量突增，连接池 maxPoolSize 偏小且存在慢 SQL 占用", "临时上调 max_connections 与连接池；治理慢 SQL 后恢复", "P1"),
    ("order-service", "ERR_6001", "order-service 调用库存服务超时", "下单链路 15% 请求超时", "库存服务 GC 停顿，未配熔断", "为下游配置 500ms 超时与熔断，扩容库存服务", "P2"),
    ("payment-service", "ERR_7010", "支付对账失败：渠道账单延迟", "对账任务连续 3 天差异 1200 笔", "渠道账单文件延迟 2 小时且格式变更", "手工补单修复状态，兼容新格式后恢复", "P1"),
    ("payment-service", "ERR_7011", "渠道查询接口超时引发补单失败", "补单任务大量 ERR_7011", "渠道接口限流，重试未退避", "增加指数退避重试与限流适配", "P2"),
    ("user-service", "ERR_9001", "用户服务 Pod OOMKilled", "Pod 每 40 分钟重启一次", "本地缓存无界增长", "缓存加 TTL 与容量上限，limit 上调至 2Gi", "P2"),
    ("api-gateway-service", "ERR_6002", "网关 502：上游发布期间连接拒绝", "网关错误率 3%", "发布窗口上游实例未配就绪探针", "摘除未就绪实例，补齐 readinessProbe", "P2"),
    ("notify-service", "ERR_9101", "通知服务消费积压 200 万条", "消息延迟 30 分钟+", "消费者实例数少于分区数", "扩容消费者至分区数，优化批量写库", "P2"),
    ("order-service", "ERR_5004", "订单服务连接获取超时", "ERR_5004 频繁出现", "DB 慢查询占用连接", "为订单查询补索引，慢查询下降 90%", "P2"),
]


def seed_all(db, container, demo_users: bool = False) -> dict:
    created = {"tenants": 0, "users": 0, "docs": 0, "faults": 0}

    for tenant_id, name in (("default", "默认租户"), ("acme", "Acme 公司")):
        if not db.get(Tenant, tenant_id):
            db.add(Tenant(id=tenant_id, name=name))
            created["tenants"] += 1
    db.commit()

    settings = get_settings()
    users = USERS if demo_users else [("default", settings.admin_username, settings.admin_password, "admin")]
    user_ids: dict[str, str] = {}
    for tenant_id, username, password, role in users:
        if tenant_id == "default" and username == settings.admin_username:
            password = settings.admin_password
        if not db.query(User).filter(User.username == username).first():
            user = User(tenant_id=tenant_id, username=username, password_hash=hash_password(password), role=role)
            db.add(user)
            db.commit()
            created["users"] += 1
        user_ids[username] = db.query(User).filter(User.username == username).first().id
    if not db.query(User).filter(User.username == settings.admin_username).first():
        user = User(tenant_id="default", username=settings.admin_username, password_hash=hash_password(settings.admin_password), role="admin")
        db.add(user)
        db.commit()
        user_ids[settings.admin_username] = user.id

    admin_id = user_ids.get(settings.admin_username, user_ids.get("admin", ""))
    for filename, title in KNOWLEDGE_DOCS:
        exists = db.query(Document).filter(Document.tenant_id == "default", Document.title == title).first()
        if exists:
            continue
        content = (KNOWLEDGE_DIR / filename).read_text(encoding="utf-8")
        doc = Document(tenant_id="default", title=title, content=content, source=f"knowledge/{filename}", created_by=admin_id)
        kb_service.ingest_document(db, container, doc)
        created["docs"] += 1

    # acme 租户放入一篇不同文档，用于演示租户隔离
    if not db.query(Document).filter(Document.tenant_id == "acme", Document.title == "Acme 内部值班手册").first():
        doc = Document(
            tenant_id="acme",
            title="Acme 内部值班手册",
            content="# Acme 内部值班手册\n\n值班响应时间为 5 分钟，升级路径：值班工程师 → 技术主管 → CTO。",
            source="knowledge/acme_oncall.md",
            created_by=user_ids.get("acme_admin", ""),
        )
        kb_service.ingest_document(db, container, doc)
        created["docs"] += 1

    if db.query(FaultRecord).filter(FaultRecord.tenant_id == "default").count() == 0:
        now = datetime.now(timezone.utc)
        for i, (service, code, title, symptom, cause, fix, severity) in enumerate(FAULT_RECORDS):
            db.add(
                FaultRecord(
                    tenant_id="default",
                    service=service,
                    error_code=code,
                    title=title,
                    symptom=symptom,
                    root_cause=cause,
                    resolution=fix,
                    severity=severity,
                    occurred_at=now - timedelta(days=7 * (i + 1)),
                )
            )
            created["faults"] += 1
        db.commit()

    return created


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo-users", action="store_true", help="额外创建仅供本机演示的固定账号")
    args = parser.parse_args()
    init_db()
    container = get_container()
    with SessionLocal() as db:
        result = seed_all(db, container, demo_users=args.demo_users)
    logger.info(
        "种子数据完成：新增租户 %d、用户 %d、文档 %d、故障记录 %d",
        result["tenants"],
        result["users"],
        result["docs"],
        result["faults"],
    )
    logger.info("管理员遵循ADMIN_USERNAME/ADMIN_PASSWORD配置；额外演示账号：%s", args.demo_users)


if __name__ == "__main__":
    main()
