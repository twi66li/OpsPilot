"""pytest 公共夹具：全部使用离线 Mock（内存 SQLite / 进程内向量库 / MockLLM）。"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("EMBEDDING_PROVIDER", "mock")
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("QDRANT_MODE", "memory")
os.environ.setdefault("RERANK_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:1/0")  # 不可达 → 缓存退化为进程内

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


def register(client: TestClient, username: str, password: str, tenant_id: str = "default") -> dict:
    resp = client.post("/auth/register", json={"username": username, "password": password, "tenant_id": tenant_id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


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
def viewer_headers(client) -> dict:
    return register(client, "viewer_e2e", "view123456", tenant_id="default")


def new_session(client: TestClient, headers: dict, title: str = "测试会话") -> str:
    resp = client.post("/api/sessions", json={"title": title}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]
