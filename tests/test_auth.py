"""认证与权限测试。"""
from tests.conftest import login, register


def test_register_login_me(client):
    headers = register(client, "user_auth_1", "pass123456", tenant_id="t-auth")
    resp = client.get("/auth/me", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "viewer"
    assert body["tenant_id"] == "t-auth"


def test_wrong_password(client):
    register(client, "user_auth_2", "pass123456")
    resp = client.post("/auth/login", json={"username": "user_auth_2", "password": "bad-password"})
    assert resp.status_code == 401


def test_me_requires_token(client):
    resp = client.get("/auth/me")
    assert resp.status_code == 401


def test_invalid_token_rejected(client):
    resp = client.get("/auth/me", headers={"Authorization": "Bearer not-a-token"})
    assert resp.status_code == 401


def test_viewer_cannot_ingest_document(client, viewer_headers):
    resp = client.post(
        "/api/kb/documents",
        json={"title": "无权文档", "content": "内容"},
        headers=viewer_headers,
    )
    assert resp.status_code == 403


def test_admin_creates_engineer(client, admin_headers):
    resp = client.post(
        "/api/admin/users",
        json={"username": "engineer_auth", "password": "engi123456", "role": "engineer", "tenant_id": "default"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "engineer"
    headers = login(client, "engineer_auth", "engi123456")
    assert client.get("/auth/me", headers=headers).json()["role"] == "engineer"


def test_duplicate_username_rejected(client):
    register(client, "dup_user", "pass123456")
    resp = client.post("/auth/register", json={"username": "dup_user", "password": "pass123456"})
    assert resp.status_code == 400
