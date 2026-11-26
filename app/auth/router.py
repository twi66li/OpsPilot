"""认证接口：注册 / 登录 / 当前用户。"""

import hashlib
import secrets
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.deps import AuthUser, get_current_user
from app.auth.security import create_access_token, hash_password, verify_password
from app.config import get_settings
from app.db.base import SessionLocal, run_with_sqlite_retry
from app.db.models import LoginSession, Tenant, User, _now
from app.services.cache import check_rate_limit, get_cache

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=64)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    tenant_id: str
    username: str
    is_guest: bool = False


def auth_limit(request: Request, username: str = ""):
    settings = get_settings()
    peer = request.client.host if request.client else "unknown"
    keys = [f"auth:ip:{peer}"]
    if username:
        keys.append("auth:account:" + hashlib.sha256(f"{peer}:{username.casefold()}".encode()).hexdigest())
    try:
        cache = get_cache(settings)
        if not all(check_rate_limit(cache, key, settings.auth_rate_limit_per_minute) for key in keys):
            raise HTTPException(429, "尝试过于频繁，请一分钟后重试", headers={"Retry-After": "60"})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "登录服务暂不可用，请稍后重试") from exc


def issue_token(db: Session, user: User) -> TokenResponse:
    from app.services.visitor_service import VISITOR_USERNAME, is_visitor

    session = LoginSession(
        user_id=user.id,
        expires_at=_now() + timedelta(minutes=get_settings().access_token_expire_minutes),
    )
    db.add(session)
    db.commit()
    guest = is_visitor(user)
    role = "viewer" if guest else user.role
    username = VISITOR_USERNAME if guest else user.username
    token = create_access_token(user.id, username, user.tenant_id, role, session_id=session.id)
    return TokenResponse(
        access_token=token, role=role, tenant_id=user.tenant_id, username=username, is_guest=guest
    )


@router.get("/options")
def options():
    from app.services.visitor_service import VISITOR_PASSWORD, VISITOR_USERNAME

    settings = get_settings()
    return {
        "registration_enabled": settings.registration_enabled,
        "drill_enabled": settings.drill_enabled,
        "visitor": {"username": VISITOR_USERNAME, "password": VISITOR_PASSWORD} if settings.visitor_enabled else None,
    }


@router.post("/register", response_model=TokenResponse)
def register(body: RegisterRequest, request: Request):
    """公开注册：服务端创建全新隔离租户（默认 viewer 角色）。

    公开注册不能加入任何既有租户；加入既有组织仅限管理员在本租户内建号。
    """
    if not get_settings().registration_enabled:
        raise HTTPException(403, "当前部署已关闭自助注册，请联系部署者获取账号")
    auth_limit(request, body.username)
    password_hash = hash_password(body.password)
    def create_account():
        with SessionLocal() as db:
            if db.scalar(select(User.id).where(User.username == body.username)):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名已存在")
            tenant_id = uuid.uuid4().hex
            db.add(Tenant(id=tenant_id, name=f"{body.username} 的组织"))
            db.flush()
            user = User(tenant_id=tenant_id, username=body.username, password_hash=password_hash, role="viewer")
            db.add(user)
            db.flush()
            return issue_token(db, user)

    try:
        return run_with_sqlite_retry(create_account)
    except IntegrityError as exc:
        with SessionLocal() as db:
            if db.scalar(select(User.id).where(User.username == body.username)):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名已存在") from exc
        raise


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request, response: Response):
    auth_limit(request, body.username)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == body.username))
        user_id = user.id if user else ""
        stored_hash = user.password_hash if user else ""
    if not user_id or not verify_password(body.password, stored_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    from app.services.visitor_service import VISITOR_COOKIE, VISITOR_ID, VISITOR_TENANT

    if user_id == VISITOR_ID:
        if not get_settings().visitor_enabled:
            raise HTTPException(403, "访客体验已关闭")
        browser_token = request.cookies.get(VISITOR_COOKIE, "")
        if len(browser_token) != 64 or any(ch not in "0123456789abcdef" for ch in browser_token):
            browser_token = secrets.token_hex(32)
        visitor_id = hashlib.sha256(browser_token.encode()).hexdigest()

        def sign_in_visitor():
            with SessionLocal() as db:
                # 同一浏览器并发登录时先锁教学租户，再读取或创建私有身份。
                db.execute(update(Tenant).where(Tenant.id == VISITOR_TENANT).values(id=Tenant.id))
                guest = db.get(User, visitor_id)
                if guest is None:
                    guest = User(id=visitor_id, tenant_id=VISITOR_TENANT, username="~guest-" + visitor_id[:56],
                                 password_hash="", role="viewer")
                    db.add(guest)
                    db.flush()
                return issue_token(db, guest)

        result = run_with_sqlite_retry(sign_in_visitor)
        response.set_cookie(VISITOR_COOKIE, browser_token, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=30 * 24 * 3600, path="/auth/login")
        return result

    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
        return issue_token(db, user)


@router.post("/logout")
def logout(user: AuthUser = Depends(get_current_user)):
    with SessionLocal() as db:
        db.execute(update(LoginSession).where(LoginSession.id == user.session_id).values(revoked_at=_now()))
        db.commit()
    return {"logged_out": True}


class PasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=64)


@router.post("/password")
def change_password(body: PasswordIn, user: AuthUser = Depends(get_current_user)):
    with SessionLocal() as db:
        row = db.get(User, user.user_id)
        if row is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已被删除")
        stored_hash = row.password_hash
        role = row.role
    if not verify_password(body.current_password, stored_hash):
        raise HTTPException(400, "当前密码错误")
    if get_settings().app_env == "production" and role == "admin" and len(body.new_password) < 12:
        raise HTTPException(400, "生产环境管理员密码至少12字符")
    new_hash = hash_password(body.new_password)
    with SessionLocal() as db:
        row = db.get(User, user.user_id)
        if row is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已被删除")
        row.password_hash = new_hash
        db.execute(
            update(LoginSession)
            .where(LoginSession.user_id == row.id, LoginSession.revoked_at.is_(None))
            .values(revoked_at=_now())
        )
        db.commit()
    return {"changed": True, "message": "密码已修改，请重新登录"}


@router.get("/me")
def me(user: AuthUser = Depends(get_current_user)):
    return {
        "user_id": user.user_id,
        "username": user.username,
        "tenant_id": user.tenant_id,
        "role": user.role,
        "is_guest": user.is_guest,
    }
