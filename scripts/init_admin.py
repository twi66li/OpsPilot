"""显式初始化管理员，重复运行不覆盖已有账号或密码。"""

from app.auth.security import hash_password
from app.config import get_settings
from app.db.base import SessionLocal, init_db
from app.db.models import LoginSession, Tenant, User, _now


def main(reset_password=False):
    init_db()
    settings = get_settings()
    with SessionLocal() as db:
        existing = db.query(User).filter_by(username=settings.admin_username).first()
        if existing and reset_password:
            if existing.role != "admin":
                raise RuntimeError("目标账号不是管理员，请先核对 ADMIN_USERNAME")
            existing.password_hash = hash_password(settings.admin_password)
            db.query(LoginSession).filter_by(user_id=existing.id).update({"revoked_at": _now()})
            db.commit()
            print("已按显式参数重设管理员密码，并撤销旧登录")
            return
        if existing:
            print("管理员账号已存在，未修改密码")
            return
        if not db.get(Tenant, "default"):
            db.add(Tenant(id="default", name="默认租户"))
            db.commit()
        db.add(
            User(
                tenant_id="default",
                username=settings.admin_username,
                role="admin",
                password_hash=hash_password(settings.admin_password),
            )
        )
        db.commit()
    print("管理员已初始化，密码从环境配置读取")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset-password", action="store_true")
    main(parser.parse_args().reset_password)
