"""显式初始化管理员，重复运行不覆盖已有账号或密码。"""
from app.auth.security import hash_password
from app.config import get_settings
from app.db.base import SessionLocal, init_db
from app.db.models import Tenant, User


def main():
    init_db()
    settings = get_settings()
    with SessionLocal() as db:
        if db.query(User).filter_by(username=settings.admin_username).first():
            print("管理员账号已存在，未修改密码")
            return
        if not db.get(Tenant, "default"):
            db.add(Tenant(id="default", name="默认租户"))
            db.commit()
        db.add(User(tenant_id="default", username=settings.admin_username, role="admin",
                    password_hash=hash_password(settings.admin_password)))
        db.commit()
    print("管理员已初始化，密码从环境配置读取")


if __name__ == "__main__":
    main()
