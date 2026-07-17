"""用户与 API Key 模型。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlmodel import Field

from cpustack.schemas.common import ActiveRecordMixin, TimestampMixin


class UserRole(str, Enum):
    ADMIN = "admin"
    USER = "user"


class User(TimestampMixin, ActiveRecordMixin, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True, nullable=False, max_length=64)
    password_hash: str = Field(nullable=False, max_length=256)
    # 角色（RBAC）：admin 可管理一切，user 仅可使用自己创建的资源
    role: UserRole = Field(default=UserRole.USER, nullable=False)
    # 向后兼容：is_admin 等价于 role == ADMIN
    is_admin: bool = Field(default=False, nullable=False)
    enabled: bool = Field(default=True, nullable=False)


class APIKey(TimestampMixin, ActiveRecordMixin, table=True):
    __tablename__ = "api_keys"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(nullable=False, max_length=128)
    access_token: str = Field(unique=True, index=True, nullable=False, max_length=128)
    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)
    # 模型白名单，空表示不限制
    allowed_model_names: str | None = Field(default=None, max_length=2048)
    expires_at: datetime | None = Field(default=None, nullable=True)
    enabled: bool = Field(default=True, nullable=False)
