"""诊断工作台前端挂载冒烟：页面可达且包含关键 DOM，API 路由不受影响。"""
import re

from fastapi.testclient import TestClient

from app.main import _FRONTEND_DIR, app


def test_frontend_index_served():
    client = TestClient(app)
    r = client.get("/")
    assert r.headers.get("cache-control") == "no-store"
    if not _FRONTEND_DIR.is_dir():
        assert r.status_code == 503 and "npm run build" in r.text
        return
    assert r.status_code == 200
    assert "OpsPilot 诊断工作台" in r.text
    asset = re.search(r'src="([^"]+\.js)"', r.text)
    assert asset and client.get(asset[1]).status_code == 200


def test_api_routes_take_precedence_over_static_mount():
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/docs").status_code == 200
    # 未带凭据调用 API 应得 401/403，而不是落入静态目录的 404
    assert client.post("/api/agent/chat", json={"session_id": "x", "message": "hi"}).status_code in (401, 403)
