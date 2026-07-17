"""Alembic 迁移环境配置。"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 确保 backend 目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpustack.config import settings  # noqa: E402
from cpustack.schemas.common import SQLModel  # noqa: E402

# 导入所有模型，确保 Alembic 能检测到
from cpustack.schemas import users, workers, models  # noqa: E402, F401

config = context.config

# 从环境变量覆盖数据库 URL
config.set_main_option("sqlalchemy.url", settings.db_url_sync)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# SQLModel.metadata 作为目标
target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 脚本。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：直接执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
