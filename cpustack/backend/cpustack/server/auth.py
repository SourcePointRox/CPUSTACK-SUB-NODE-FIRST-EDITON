"""认证与鉴权：JWT + API Key 双模式。"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPBearer
from sqlmodel import select

from cpustack.config import settings
from cpustack.db import get_session
from cpustack.schemas.users import APIKey, User, UserRole

logger = logging.getLogger(__name__)
_hasher = PasswordHasher()

# API Key 请求头
_api_key_header = APIKeyHeader(name="Authorization", auto_error=False, scheme_name="Bearer")
_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    """Argon2 哈希密码。"""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """验证密码。"""
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        logger.exception("密码验证异常")
        return False


def create_jwt_token(user_id: int, username: str, is_admin: bool, role: str = "user") -> str:
    """创建 JWT Token。"""
    payload = {
        "sub": str(user_id),
        "username": username,
        "is_admin": is_admin,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expire_hours),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_jwt_token(token: str) -> dict | None:
    """解码 JWT Token。"""
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def generate_api_key() -> str:
    """生成 API Key（sk- 前缀）。"""
    return "sk-" + secrets.token_hex(24)


async def get_current_user(
    authorization: str | None = Depends(_api_key_header),
    session=Depends(get_session),
) -> User:
    """获取当前认证用户。

    支持两种认证方式：
    1. JWT Bearer Token（管理界面）
    2. API Key（sk-xxx，程序调用）
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少认证信息",
        )

    token = authorization.removeprefix("Bearer ").strip()

    # 尝试 API Key 认证
    if token.startswith("sk-"):
        stmt = select(APIKey).where(
            APIKey.access_token == token,
            APIKey.enabled == True,  # noqa: E712
        )
        api_key = (await session.execute(stmt)).scalar_one_or_none()
        if not api_key:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的 API Key")
        if api_key.expires_at and api_key.expires_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API Key 已过期")

        user_stmt = select(User).where(User.id == api_key.user_id, User.enabled == True)  # noqa: E712
        user = (await session.execute(user_stmt)).scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不可用")
        # 关联 API Key 到 user（供模型白名单校验）
        object.__setattr__(user, "api_key", api_key)
        return user

    # JWT Token 认证
    payload = decode_jwt_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的 Token")

    user_id = int(payload["sub"])
    user_stmt = select(User).where(User.id == user_id, User.enabled == True)  # noqa: E712
    user = (await session.execute(user_stmt)).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不可用")
    return user


def _is_admin(user: User) -> bool:
    """判断用户是否为管理员（兼容 role 和 is_admin 两个字段）。"""
    return user.is_admin or user.role == UserRole.ADMIN


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """要求管理员权限。"""
    if not _is_admin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return user


def require_role(*allowed_roles: UserRole):
    """要求用户角色在允许列表中（RBAC 依赖工厂）。

    用法：@router.get(..., dependencies=[Depends(require_role(UserRole.ADMIN))])
    """
    async def _check(user: User = Depends(get_current_user)) -> User:
        if _is_admin(user):
            return user  # admin 隐式拥有所有角色权限
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要以下角色之一: {[r.value for r in allowed_roles]}",
            )
        return user
    return _check
