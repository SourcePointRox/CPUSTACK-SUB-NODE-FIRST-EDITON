"""add user role for rbac

Revision ID: b7e9d1f4a302
Revises: a3f5c2e8b901
Create Date: 2026-06-28 00:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'b7e9d1f4a302'
down_revision: Union[str, None] = 'a3f5c2e8b901'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. 添加 role 列（默认 'user'）
    op.add_column(
        'users',
        sa.Column(
            'role',
            sa.Enum('admin', 'user', name='userrole'),
            nullable=False,
            server_default='user',
        ),
    )

    # 2. 数据迁移：现有 is_admin=True 的用户 role 设为 'admin'
    op.execute("UPDATE users SET role='admin' WHERE is_admin=1")


def downgrade() -> None:
    op.drop_column('users', 'role')
