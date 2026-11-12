"""认证接口：注册 / 登录 / 当前用户。"""
import hashlib
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.auth.deps import AuthUser, get_current_user
from app.auth.security import create_access_token, hash_password, verify_password
from app.config import get_settings
from app.db.async_session import get_async_db
from app.db.models import LoginSession, Tenant, User, _now
from app.services.cache import check_rate_limit, get_cache

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


def auth_limit(request: Request, username: str = ""):
    settings = get_settings()
    peer = request.client.host if request.client else "unknown"
    keys = [f"auth:ip:{peer}"]
    if username:
        keys.append("auth:account:" + hashlib.sha256(f"{peer}:{username.casefold()}".encode()).hexdigest())
    try:
        cache = get_cache(settings)
        if not all(check_rate_limit(cache, key, settings.auth_rate_limit_per_minute) for key in keys):
            raise HTTPException(429, "尝试过于频繁，请一分钟后重试", headers={"Retry-After":"60"})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "登录服务暂不可用，请稍后重试") from exc


async def issue_token(db, user):
    session = LoginSession(user_id=user.id, expires_at=_now() + timedelta(minutes=get_settings().access_token_expire_minutes))
    db.add(session)
    await db.commit()
    token = create_access_token(user.id, user.username, user.tenant_id, user.role, session_id=session.id)
    return TokenResponse(access_token=token, role=user.role, tenant_id=user.tenant_id, username=user.username)


@router.get("/options")
def options():
    return {"registration_enabled":get_settings().registration_enabled}


@router.post("/register", response_model=TokenResponse)
async def register(body: RegisterRequest, request: Request, db: AsyncSession = Depends(get_async_db)):
    """公开注册：服务端创建全新隔离租户（默认 viewer 角色）。

    公开注册不能加入任何既有租户；加入既有组织仅限管理员在本租户内建号。
    """
    if not get_settings().registration_enabled:
        raise HTTPException(403, "当前部署已关闭自助注册，请联系部署者获取账号")
    auth_limit(request, body.username)
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
    return await issue_token(db, user)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_async_db)):
    auth_limit(request, body.username)
    user = await db.scalar(select(User).where(User.username == body.username))
    if not user or not await run_in_threadpool(verify_password, body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    return await issue_token(db, user)


@router.post("/logout")
async def logout(user: AuthUser = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    await db.execute(update(LoginSession).where(LoginSession.id == user.session_id).values(revoked_at=_now()))
    await db.commit()
    return {"logged_out":True}


class PasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=64)


@router.post("/password")
async def change_password(body: PasswordIn, user: AuthUser = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    row = await db.get(User, user.user_id)
    if not await run_in_threadpool(verify_password, body.current_password, row.password_hash):
        raise HTTPException(400, "当前密码错误")
    if get_settings().app_env == "production" and row.role == "admin" and len(body.new_password) < 12:
        raise HTTPException(400, "生产环境管理员密码至少12字符")
    row.password_hash = await run_in_threadpool(hash_password, body.new_password)
    await db.execute(update(LoginSession).where(LoginSession.user_id == row.id, LoginSession.revoked_at.is_(None)).values(revoked_at=_now()))
    await db.commit()
    return {"changed":True, "message":"密码已修改，请重新登录"}


@router.get("/me")
async def me(user: AuthUser = Depends(get_current_user)):
    return {"user_id": user.user_id, "username": user.username, "tenant_id": user.tenant_id, "role": user.role}
