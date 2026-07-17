"""一次性数据库初始化脚本：用 SQLModel.metadata.create_all 建表。

适用于 sqlite 本地开发环境（避开 alembic 的 db_url_sync PostgreSQL 配置）。
运行: python init_db.py
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("init_db")

# 确保 Windows 友好的本地目录
os.environ.setdefault("CPUSTACK_DATA_DIR", "./data")
os.environ.setdefault("CPUSTACK_MODEL_CACHE_DIR", "./data/cache")

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel

# 导入所有模型，确保 metadata 注册
from cpustack.schemas import users, workers, models  # noqa: F401, E402
from cpustack.schemas import tokens  # noqa: F401, E402
from cpustack.schemas import knowledge  # noqa: F401, E402
from cpustack.config import settings


async def init() -> None:
    logger.info("数据库 URL: %s", settings.db_url)
    engine: AsyncEngine = create_async_engine(settings.db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    await engine.dispose()
    logger.info("数据库表创建完成")

    # 列出创建的表
    engine2 = create_async_engine(settings.db_url, echo=False)
    async with engine2.connect() as conn:
        from sqlalchemy import text
        result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
        tables = [row[0] for row in result]
        logger.info("已创建表: %s", tables)
    await engine2.dispose()


if __name__ == "__main__":
    asyncio.run(init())
