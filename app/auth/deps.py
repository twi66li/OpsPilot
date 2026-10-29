"""FastAPI 依赖：当前用户解析与角色权限检查。"""
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.security import decode_token
from app.db.base import get_db
from app.db.models import User

_bearer = HTTPBearer(auto_error=False)

VALID_ROLES = ("admin", "engineer", "viewer")


@dataclass
class AuthUser:
    user_id: str
    username: str
    tenant_id: str
    role: str


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> AuthUser:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 Bearer Token")
    try:
        payload = decode_token(credentials.credentials)
    except Exception as exc:  # jwt 过期/签名错误
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Token 无效: {exc}") from exc
    user = db.get(User, payload.get("sub", ""))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已被删除")
    return AuthUser(user_id=user.id, username=user.username, tenant_id=user.tenant_id, role=user.role)


def require_roles(*roles: str):
    def checker(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"需要角色 {'/'.join(roles)}，当前为 {user.role}")
        return user

    return checker
