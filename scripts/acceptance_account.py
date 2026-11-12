"""验收用独立账号；不再把站点和对话写入管理员工作区。"""
import os
import secrets
import uuid

from app.config import get_settings


def login_acceptance(client, *, engineer=False):
    username, password = os.getenv("ACCEPTANCE_USERNAME"), os.getenv("ACCEPTANCE_PASSWORD")
    if bool(username) != bool(password):
        raise ValueError("继续既有验收时须同时设置 ACCEPTANCE_USERNAME 和 ACCEPTANCE_PASSWORD")
    if username:
        response = client.post("/auth/login", json={"username": username, "password": password})
    else:
        username, password = "acceptance_" + uuid.uuid4().hex[:16], secrets.token_urlsafe(24)
        if engineer:
            settings = get_settings()
            admin = client.post("/auth/login", json={"username": settings.admin_username, "password": settings.admin_password})
            admin.raise_for_status()
            created = client.post("/api/admin/users", headers={"Authorization": "Bearer " + admin.json()["access_token"]},
                                  json={"username": username, "password": password, "role": "engineer",
                                        "tenant_id": admin.json()["tenant_id"]})
            created.raise_for_status()
            response = client.post("/auth/login", json={"username": username, "password": password})
        else:
            response = client.post("/auth/register", json={"username": username, "password": password})
    response.raise_for_status()
    client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
    return username
