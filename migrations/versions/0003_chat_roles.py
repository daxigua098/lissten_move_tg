"""为聊天对象增加源/接收组角色字段

Revision ID: 0003_chat_roles
Revises: 0002_telegram_resources
Create Date: 2026-09-25

同一个群/频道既可能是监听源、也可能是接收组，因此用角色标记而不是拆表：
is_source / source_enabled / is_target / target_enabled / target_role。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003_chat_roles"
down_revision: str | None = "0002_telegram_resources"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # SQLite 不支持直接 ALTER，用 batch 模式重建表
    with op.batch_alter_table("chats") as batch_op:
        batch_op.add_column(
            sa.Column("is_source", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("source_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch_op.add_column(
            sa.Column("is_target", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("target_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch_op.add_column(
            sa.Column(
                "target_role",
                sa.String(length=16),
                nullable=False,
                server_default="content",
            )
        )
        batch_op.create_index("ix_chats_is_source", ["is_source"], unique=False)
        batch_op.create_index("ix_chats_is_target", ["is_target"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("chats") as batch_op:
        batch_op.drop_index("ix_chats_is_target")
        batch_op.drop_index("ix_chats_is_source")
        batch_op.drop_column("target_role")
        batch_op.drop_column("target_enabled")
        batch_op.drop_column("is_target")
        batch_op.drop_column("source_enabled")
        batch_op.drop_column("is_source")
