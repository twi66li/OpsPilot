"""认证与权限测试。"""
from tests.conftest import login, register


def test_register_login_me(client):
    headers = register(client, "user_auth_1", "pass123456")
    resp = client.get("/auth/me", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "viewer"
    assert body["tenant_id"]  # 服务端生成的隔离租户，而非客户端指定


def test_register_cannot_join_existing_tenant(client, admin_headers):
    """回归：匿名注册携带既有租户 ID，不得加入该租户读取其私有文档。"""
    resp = client.post(
        "/api/kb/documents",
        json={"title": "default 私有手册", "content": "机密：仅 default 租户可见的处理手册"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    reg = client.post("/auth/register", json={"username": "outsider_1", "password": "pass123456", "tenant_id": "default"})
    assert reg.status_code == 200, reg.text
    body = reg.json()
    assert body["tenant_id"] != "default"
    listed = client.get("/api/kb/documents", headers={"Authorization": f"Bearer {body['access_token']}"}).json()
    assert all(d["title"] != "default 私有手册" for d in listed)


def test_admin_cannot_create_user_in_other_tenant(client, admin_headers):
    resp = client.post(
        "/api/admin/users",
        json={"username": "intruder", "password": "pass123456", "role": "engineer", "tenant_id": "other-org"},
        headers=admin_headers,
    )
    assert resp.status_code == 403
    assert client.post("/auth/login", json={"username": "intruder", "password": "pass123456"}).status_code == 401


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


def test_same_tenant_users_cannot_access_another_users_session_or_trace(
    client, admin_headers, viewer_headers, engineer_headers
):
    from tests.conftest import new_session

    sid = new_session(client, admin_headers, "个人调查")
    answer = client.post("/api/agent/chat", json={"session_id": sid, "message": "连接池如何排查"}, headers=admin_headers)
    assert answer.status_code == 200
    run_id = answer.json()["run_id"]
    for headers in (viewer_headers, engineer_headers):
        assert sid not in {s["id"] for s in client.get("/api/sessions", headers=headers).json()}
        for path in (f"/api/sessions/{sid}", f"/api/agent/runs/{run_id}", f"/api/agent/sessions/{sid}/pending-approval"):
            assert client.get(path, headers=headers).status_code == 404
        for path in ("/api/agent/chat", "/api/agent/chat/stream"):
            assert client.post(path, json={"session_id": sid, "message": "继续调查"}, headers=headers).status_code == 404
    assert client.get(f"/api/sessions/{sid}", headers=admin_headers).status_code == 200
    assert client.get(f"/api/agent/runs/{run_id}", headers=admin_headers).status_code == 200
