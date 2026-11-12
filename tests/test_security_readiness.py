from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text

from app.config import Settings, get_settings
from tests.conftest import register


def test_production_rejects_defaults():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production")
    settings = Settings(_env_file=None, app_env="production", jwt_secret="x" * 40, admin_password="long-unique-password")
    assert settings.cors_origins == []


def test_revocation_password_change_and_registration_switch(client, monkeypatch):
    headers = register(client, "security_sessions", "pass123456")
    other = client.post("/auth/login", json={"username":"security_sessions","password":"pass123456"}).json()
    assert client.post("/auth/logout", headers=headers).status_code == 200
    assert client.get("/auth/me", headers=headers).status_code == 401
    headers = {"Authorization":"Bearer " + other["access_token"]}
    assert client.post("/auth/password", headers=headers, json={"current_password":"wrong", "new_password":"newpass123456"}).status_code == 400
    assert client.post("/auth/password", headers=headers, json={"current_password":"pass123456", "new_password":"newpass123456"}).status_code == 200
    assert client.get("/auth/me", headers=headers).status_code == 401
    assert client.post("/auth/login", json={"username":"security_sessions", "password":"newpass123456"}).status_code == 200
    monkeypatch.setattr(get_settings(), "registration_enabled", False)
    assert client.get("/auth/options").json()["registration_enabled"] is False
    assert client.post("/auth/register", json={"username":"not_allowed", "password":"pass123456"}).status_code == 403


def test_login_has_its_own_rate_limit(client, monkeypatch):
    from app.auth import router
    from app.services.cache import InMemoryCache

    cache = InMemoryCache()
    monkeypatch.setattr(router, "get_cache", lambda settings: cache)
    monkeypatch.setattr(get_settings(), "auth_rate_limit_per_minute", 1)
    assert client.post("/auth/login", json={"username":"not_exist", "password":"wrong"}).status_code == 401
    response = client.post("/auth/login", json={"username":"not_exist", "password":"wrong"})
    assert response.status_code == 429 and response.headers["Retry-After"] == "60"


def test_liveness_and_readiness_are_distinct(client, monkeypatch):
    from app.services import ingestion_service

    assert client.get("/live").status_code == 200
    assert client.get("/ready").status_code == 200
    monkeypatch.setattr(ingestion_service, "worker_ready", lambda: False)
    assert client.get("/ready").status_code == 503
    assert client.get("/live").status_code == 200


def test_migration_adopts_old_database_without_resetting_data(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "legacy.db"))
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE tenants (id VARCHAR(64) PRIMARY KEY, name VARCHAR(128) NOT NULL, created_at DATETIME NOT NULL)"))
        conn.execute(text("INSERT INTO tenants VALUES ('kept', 'existing', CURRENT_TIMESTAMP)"))
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
        assert conn.scalar(text("SELECT name FROM tenants WHERE id='kept'")) == "existing"
        assert "login_sessions" in inspect(conn).get_table_names()
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0001"
        command.upgrade(config, "head")
