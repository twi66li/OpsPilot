"""认证接口：注册 / 登录 / 当前用户。"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.auth.deps import AuthUser, get_current_user
from app.auth.security import create_access_token, hash_password, verify_password
from app.db.async_session import get_async_db
from app.db.models import Tenant, User

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=64)


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
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_async_db)):
    """公开注册：服务端创建全新隔离租户（默认 viewer 角色）。

    公开注册不能加入任何既有租户；加入既有组织仅限管理员在本租户内建号。
    """
    if await db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名已存在")
    tenant_id = uuid.uuid4().hex
    db.add(Tenant(id=tenant_id, name=f"{body.username} 的组织"))
    user = User(
        tenant_id=tenant_id,
        username=body.username,
        password_hash=await run_in_threadpool(hash_password, body.password),
        role="viewer",
    )
    db.add(user)
    await db.commit()
    token = create_access_token(user.id, user.username, user.tenant_id, user.role)
    return TokenResponse(access_token=token, role=user.role, tenant_id=user.tenant_id, username=user.username)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_async_db)):
    user = await db.scalar(select(User).where(User.username == body.username))
    if not user or not await run_in_threadpool(verify_password, body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    token = create_access_token(user.id, user.username, user.tenant_id, user.role)
    return TokenResponse(access_token=token, role=user.role, tenant_id=user.tenant_id, username=user.username)


@router.get("/me")
async def me(user: AuthUser = Depends(get_current_user)):
    return {"user_id": user.user_id, "username": user.username, "tenant_id": user.tenant_id, "role": user.role}
