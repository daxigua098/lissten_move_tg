"""热门关键词表：从会员发言里采词并累计，永不删除

Revision ID: 0012_hot_keywords
Revises: 0011_group_kind
Create Date: 2026-09-26

只做累加（token 唯一 + count），不参与 retention 清理，
用于给词库补充"用户真正在搜的词"。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012_hot_keywords"
down_revision: str | None = "0011_group_kind"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "hot_keywords",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sources", sa.Text(), nullable=False, server_default=""),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hot_keywords")),
    )
    op.create_index(op.f("ix_hot_keywords_token"), "hot_keywords", ["token"], unique=True)
    op.create_index(op.f("ix_hot_keywords_count"), "hot_keywords", ["count"], unique=False)


def downgrade() -> None:
    op.drop_table("hot_keywords")
