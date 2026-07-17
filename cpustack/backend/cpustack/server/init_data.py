"""初始化默认数据：管理员用户。"""

from __future__ import annotations

import logging

from sqlmodel import select

from cpustack.db import session_scope
from cpustack.schemas.users import User
from cpustack.server.auth import hash_password

logger = logging.getLogger(__name__)

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "cpustack"


async def init_default_data() -> None:
    """初始化默认管理员账户（首次启动时）。"""
    async with session_scope() as session:
        stmt = select(User).where(User.username == DEFAULT_ADMIN_USERNAME)
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing:
            return

        admin = User(
            username=DEFAULT_ADMIN_USERNAME,
            password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
            is_admin=True,
            enabled=True,
        )
        session.add(admin)
        logger.info(
            "已创建默认管理员账户: %s / %s（请及时修改密码）",
            DEFAULT_ADMIN_USERNAME,
            DEFAULT_ADMIN_PASSWORD,
        )
