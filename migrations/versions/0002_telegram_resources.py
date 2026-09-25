"""初始化 Telegram 资源表

Revision ID: 0002_telegram_resources
Revises: 0001_init_auth
Create Date: 2026-09-25

创建 tg_accounts（执行账号）、control_bots（控制 Bot）、chats（聊天对象）。
字段定义与《数据字典_字段级.md》第 2 章一致。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002_telegram_resources"
down_revision: str | None = "0001_init_auth"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "tg_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("phone_masked", sa.String(length=32), nullable=False),
        sa.Column("phone_enc", sa.String(length=255), nullable=False),
        sa.Column("api_id_enc", sa.String(length=255), nullable=False),
        sa.Column("api_hash_enc", sa.String(length=255), nullable=False),
        sa.Column("session_name", sa.String(length=128), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("health_score", sa.Integer(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tg_accounts")),
    )
    op.create_index("ix_tg_accounts_name", "tg_accounts", ["name"], unique=True)
    op.create_index("ix_tg_accounts_status", "tg_accounts", ["status"], unique=False)

    op.create_table(
        "control_bots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("bot_username", sa.String(length=64), nullable=True),
        sa.Column("bot_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("token_enc", sa.String(length=255), nullable=False),
        sa.Column("admin_ids", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_control_bots")),
    )
    op.create_index("ix_control_bots_name", "control_bots", ["name"], unique=True)

    op.create_table(
        "chats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_type", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("is_private", sa.Boolean(), nullable=False),
        sa.Column("joined", sa.Boolean(), nullable=False),
        sa.Column("can_post", sa.Boolean(), nullable=True),
        sa.Column("member_count", sa.Integer(), nullable=True),
        sa.Column("tags", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chats")),
    )
    op.create_index("ix_chats_tg_id", "chats", ["tg_id"], unique=True)
    op.create_index("ix_chats_chat_type", "chats", ["chat_type"], unique=False)
    op.create_index("ix_chats_source_kind", "chats", ["source_kind"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_chats_source_kind", table_name="chats")
    op.drop_index("ix_chats_chat_type", table_name="chats")
    op.drop_index("ix_chats_tg_id", table_name="chats")
    op.drop_table("chats")

    op.drop_index("ix_control_bots_name", table_name="control_bots")
    op.drop_table("control_bots")

    op.drop_index("ix_tg_accounts_status", table_name="tg_accounts")
    op.drop_index("ix_tg_accounts_name", table_name="tg_accounts")
    op.drop_table("tg_accounts")
