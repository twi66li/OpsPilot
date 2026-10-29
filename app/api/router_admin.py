"""管理员接口：用户管理。"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import require_roles
from app.auth.security import hash_password
from app.db.base import get_db
from app.db.models import Tenant, User

router = APIRouter(prefix="/api/admin", tags=["admin"])


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=64)
    role: str = Field(default="engineer")  # admin | engineer | viewer
    tenant_id: str = "default"


@router.post("/users")
def create_user(body: UserIn, _: object = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    """管理员创建用户（可指定角色与租户）。"""
    if body.role not in ("admin", "engineer", "viewer"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "role 必须是 admin/engineer/viewer")
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名已存在")
    if not db.get(Tenant, body.tenant_id):
        db.add(Tenant(id=body.tenant_id, name=body.tenant_id))
    user = User(tenant_id=body.tenant_id, username=body.username, password_hash=hash_password(body.password), role=body.role)
    db.add(user)
    db.commit()
    return {"id": user.id, "username": user.username, "role": user.role, "tenant_id": user.tenant_id}
