"""FastAPI 依赖：当前用户解析与角色权限检查。"""

from dataclasses import dataclass, replace

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.security import decode_token
from app.db.base import SessionLocal
from app.db.models import LoginSession, User, _now

_bearer = HTTPBearer(auto_error=False)

VALID_ROLES = ("admin", "engineer", "viewer")


@dataclass
class AuthUser:
    user_id: str
    username: str
    tenant_id: str
    role: str
    session_id: str = ""
    is_guest: bool = False


def authenticated_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthUser:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 Bearer Token")
    try:
        payload = decode_token(credentials.credentials)
    except Exception as exc:  # jwt 过期/签名错误
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Token 无效: {exc}") from exc
    if payload.get("token_use") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效，请重新登录")
    with SessionLocal() as db:
        user = db.get(User, payload.get("sub", ""))
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已被删除")
        session = db.get(LoginSession, payload.get("jti", ""))
        if not session or session.user_id != user.id or session.revoked_at or session.expires_at <= _now():
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效，请重新登录")
        from app.config import get_settings
        from app.services.visitor_service import VISITOR_USERNAME, is_visitor

        guest = is_visitor(user)
        user_id = user.id
        username = VISITOR_USERNAME if guest else user.username
        tenant_id = user.tenant_id
        role = "viewer" if guest else user.role
        session_id = session.id
    if guest:
        if not get_settings().visitor_enabled and request.url.path != "/auth/logout":
            raise HTTPException(403, "访客体验已关闭")
    return AuthUser(
        user_id=user_id,
        username=username,
        tenant_id=tenant_id,
        role=role,
        session_id=session_id,
        is_guest=guest,
    )


def get_current_user(request: Request, user: AuthUser = Depends(authenticated_user)) -> AuthUser:
    if user.is_guest:
        from app.services.visitor_service import VISITOR_ID

        if request.method not in ("GET", "HEAD") and request.url.path != "/auth/logout":
            raise HTTPException(403, "教学区为只读体验，请使用个人账号修改或发起排查")
        return replace(user, user_id=VISITOR_ID)
    return user


def get_assistant_user(user: AuthUser = Depends(authenticated_user)) -> AuthUser:
    from app.services.visitor_service import VISITOR_ID

    if user.is_guest and user.user_id == VISITOR_ID:
        raise HTTPException(401, "访客身份已更新，请重新进入")
    return user


def require_roles(*roles: str):
    def checker(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"需要角色 {'/'.join(roles)}，当前为 {user.role}")
        return user

    return checker
