from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.auth.security import verify_password
from app.db.base import Base, _make_engine
from app.db.models import User
from scripts import seed


def test_deployment_seed_uses_configured_admin_without_fixed_demo_accounts(monkeypatch):
    engine = _make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(seed, "get_settings", lambda: SimpleNamespace(admin_username="deploy_admin", admin_password="deployment-secret"))

    def ingest(db, container, doc):
        db.add(doc)
        db.commit()
        return 0

    monkeypatch.setattr(seed.kb_service, "ingest_document", ingest)
    with Session(engine) as db:
        seed.seed_all(db, None)
        users = db.query(User).all()
        assert [user.username for user in users] == ["deploy_admin"]
        assert verify_password("deployment-secret", users[0].password_hash)
    engine.dispose()
