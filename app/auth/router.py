"""认证接口：注册 / 登录 / 当前用户。"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import AuthUser, get_current_user
from app.auth.security import create_access_token, hash_password, verify_password
from app.db.base import get_db
from app.db.models import Tenant, User

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=64)
    tenant_id: str = "default"


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    tenant_id: str
    username: str


@router.post("/register", response_model=TokenResponse)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    """注册新用户（默认 viewer 角色，所属租户不存在则自动创建）。"""
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名已存在")
    if not db.get(Tenant, body.tenant_id):
        db.add(Tenant(id=body.tenant_id, name=body.tenant_id))
    user = User(
        tenant_id=body.tenant_id,
        username=body.username,
        password_hash=hash_password(body.password),
        role="viewer",
    )
    db.add(user)
    db.commit()
    token = create_access_token(user.id, user.username, user.tenant_id, user.role)
    return TokenResponse(access_token=token, role=user.role, tenant_id=user.tenant_id, username=user.username)


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    token = create_access_token(user.id, user.username, user.tenant_id, user.role)
    return TokenResponse(access_token=token, role=user.role, tenant_id=user.tenant_id, username=user.username)


@router.get("/me")
def me(user: AuthUser = Depends(get_current_user)):
    return {"user_id": user.user_id, "username": user.username, "tenant_id": user.tenant_id, "role": user.role}
