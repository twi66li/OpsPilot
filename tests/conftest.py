"""pytest 公共夹具：全部使用离线 Mock（内存 SQLite / 进程内向量库 / MockLLM）。"""
import os

# 离线契约显式钉死（不用 setdefault）：项目 .env 会被真实评测改为 MODEL_STRICT=true 等，
# 若不覆盖，strict 分支会在夹具内拒绝 mock 组件，全部测试无法离线运行。
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["EMBEDDING_PROVIDER"] = "mock"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["MODEL_STRICT"] = "false"
os.environ["QDRANT_MODE"] = "memory"
os.environ["RERANK_ENABLED"] = "false"
os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"  # 不可达 → 缓存退化为进程内
os.environ["AGENT_MODE"] = "rule"
os.environ["METRICS_PROVIDER"] = "mock"
os.environ["CHECKPOINT_PATH"] = ""
os.environ["BUDGET_LEDGER_PATH"] = ""
os.environ["CHAT_MODEL_CANDIDATES"] = "[]"
os.environ["CHAT_MODEL_EXPIRATIONS"] = "{}"
os.environ["MCP_ENABLED"] = "false"
os.environ["RETRIEVAL_SUBAGENT_ENABLED"] = "false"
os.environ["AUTH_RATE_LIMIT_PER_MINUTE"] = "10000"
os.environ["REGISTRATION_ENABLED"] = "true"

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session", autouse=True)
def seeded_docs(client, admin_headers):
    """为诊断链路测试灌入最小知识集（连接池 / 订单服务超时 / Kafka 积压）。"""
    docs = [
        ("ERR_5003 数据库连接池耗尽处理手册", "# 连接池耗尽处理\n\nERR_5003 表示数据库连接池耗尽。处理步骤：检查 maxPoolSize 配置，执行 KILL 清理 Sleep 连接，治理慢 SQL。"),
        ("order-service 接口超时排查 SOP", "# order-service 接口超时排查\n\n接口超时检查下游依赖熔断与连接池；ERR_6001 为下游调用超时，需配置超时与熔断。"),
        ("Kafka 消费积压处理", "# Kafka 消费积压处理\n\n消费组 Lag 持续增长时，扩容消费者实例并优化批量写库，检查毒消息重试。"),
    ]
    for title, content in docs:
        resp = client.post("/api/kb/documents", json={"title": title, "content": content, "source": "test"}, headers=admin_headers)
        assert resp.status_code == 200


def login(client: TestClient, username: str, password: str) -> dict:
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def register(client: TestClient, username: str, password: str) -> dict:
    """公开注册：服务端创建隔离租户，不再接受租户参数（修复后不能注册进既有租户）。"""
    resp = client.post("/auth/register", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def admin_create_user(
    client: TestClient, admin_headers: dict, username: str, password: str, role: str, tenant_id: str = "default"
) -> dict:
    """管理员在本租户内建号（加入既有租户的唯一途径，跨租户建号已被拒绝）。"""
    resp = client.post(
        "/api/admin/users",
        json={"username": username, "password": password, "role": role, "tenant_id": tenant_id},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    return login(client, username, password)


def seed_tenant_admin(client: TestClient, tenant_id: str, username: str, password: str) -> dict:
    """测试专用：直接建独立租户与管理员（等价于 scripts/seed.py 的开通路径），随后走登录。"""
    from app.auth.security import hash_password
    from app.db.base import SessionLocal
    from app.db.models import Tenant, User

    with SessionLocal() as db:
        if not db.get(Tenant, tenant_id):
            db.add(Tenant(id=tenant_id, name=f"测试租户 {tenant_id}"))
        if not db.query(User).filter(User.username == username).first():
            db.add(User(tenant_id=tenant_id, username=username, password_hash=hash_password(password), role="admin"))
        db.commit()
    return login(client, username, password)


@pytest.fixture(scope="session")
def admin_headers(client) -> dict:
    return login(client, "admin", "admin123")


@pytest.fixture(scope="session")
def engineer_headers(client, admin_headers) -> dict:
    resp = client.post(
        "/api/admin/users",
        json={"username": "engineer_e2e", "password": "engi123456", "role": "engineer", "tenant_id": "default"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    return login(client, "engineer_e2e", "engi123456")


@pytest.fixture(scope="session")
def viewer_headers(client, admin_headers) -> dict:
    return admin_create_user(client, admin_headers, "viewer_e2e", "view123456", role="viewer")


def new_session(client: TestClient, headers: dict, title: str = "测试会话") -> str:
    resp = client.post("/api/sessions", json={"title": title}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]
