"""词组支持两种用途：关键词组 / 排除词组

Revision ID: 0011_group_kind
Revises: 0010_member_pinned
Create Date: 2026-09-26

排除词库复用 keyword_groups / keywords 两张表，用 kind 区分用途：
线路可以多选引用多个排除词组，与本线路自定义的排除词取并集。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011_group_kind"
down_revision: str | None = "0010_member_pinned"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("keyword_groups") as batch_op:
        batch_op.add_column(
            sa.Column(
                "kind",
                sa.String(length=16),
                nullable=False,
                server_default="keyword",
            )
        )
        batch_op.create_index(op.f("ix_keyword_groups_kind"), ["kind"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("keyword_groups") as batch_op:
        batch_op.drop_index(op.f("ix_keyword_groups_kind"))
        batch_op.drop_column("kind")
