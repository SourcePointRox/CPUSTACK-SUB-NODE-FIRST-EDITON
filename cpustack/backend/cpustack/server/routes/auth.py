"""认证路由：登录、获取当前用户、管理 API Key。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from cpustack.db import get_session
from cpustack.schemas.users import APIKey, User
from cpustack.server.auth import (
    create_jwt_token,
    generate_api_key,
    get_current_user,
    hash_password,
    verify_password,
)

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    user: dict


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    is_admin: bool


class APIKeyCreate(BaseModel):
    name: str
    allowed_model_names: list[str] | None = None  # 模型白名单，None 表示不限制
    expires_at: datetime | None = None


class APIKeyResponse(BaseModel):
    id: int
    name: str
    access_token: str
    enabled: bool
    allowed_model_names: list[str] | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest, session=Depends(get_session)):
    """用户登录，返回 JWT Token。"""
    stmt = select(User).where(User.username == req.username, User.enabled == True)  # noqa: E712
    user = (await session.execute(stmt)).scalar_one_or_none()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = create_jwt_token(
        user.id, user.username, user.is_admin, role=user.role.value
    )
    return LoginResponse(
        access_token=token,
        user={
            "id": user.id,
            "username": user.username,
            "role": user.role.value,
            "is_admin": user.is_admin,
        },
    )


@router.get("/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_current_user)):
    """获取当前登录用户信息。"""
    return UserResponse(
        id=user.id, username=user.username, role=user.role.value, is_admin=user.is_admin
    )


@router.get("/api-keys", response_model=list[APIKeyResponse])
async def list_api_keys(
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """列出当前用户的 API Key。"""
    import json

    stmt = select(APIKey).where(APIKey.user_id == user.id, APIKey.enabled == True)  # noqa: E712
    keys = (await session.execute(stmt)).scalars().all()
    result = []
    for k in keys:
        try:
            allowed = json.loads(k.allowed_model_names) if k.allowed_model_names else None
        except (json.JSONDecodeError, TypeError):
            allowed = None
        result.append(
            APIKeyResponse(
                id=k.id, name=k.name, access_token=k.access_token,
                enabled=k.enabled, allowed_model_names=allowed,
                expires_at=k.expires_at, created_at=k.created_at,
            )
        )
    return result


@router.post("/api-keys", response_model=APIKeyResponse)
async def create_api_key(
    req: APIKeyCreate,
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """创建 API Key（可指定模型白名单）。"""
    import json

    token = generate_api_key()
    api_key = APIKey(
        name=req.name,
        access_token=token,
        user_id=user.id,
        enabled=True,
        allowed_model_names=json.dumps(req.allowed_model_names) if req.allowed_model_names else None,
        expires_at=req.expires_at,
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    return APIKeyResponse(
        id=api_key.id, name=api_key.name, access_token=api_key.access_token,
        enabled=True, allowed_model_names=req.allowed_model_names,
        expires_at=api_key.expires_at, created_at=api_key.created_at,
    )


@router.delete("/api-keys/{key_id}")
async def delete_api_key(
    key_id: int,
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """禁用 API Key。"""
    stmt = select(APIKey).where(APIKey.id == key_id, APIKey.user_id == user.id)
    api_key = (await session.execute(stmt)).scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    api_key.enabled = False
    session.add(api_key)
    await session.commit()
    return {"message": "已禁用"}
