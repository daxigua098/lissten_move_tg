"""命中关键词的用户永久保留：会员档案新增 pinned / first_hit_at

Revision ID: 0010_member_pinned
Revises: 0009_leads_survive
Create Date: 2026-09-26

保留策略改为两档：
- 命中关键词的线索（leads.keyword 非空）与对应会员档案 → 永久保留；
- 未命中的线索与档案 → 仍按 retention.leads_days / member_profiles_days 清理。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010_member_pinned"
down_revision: str | None = "0009_leads_survive"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("member_profiles") as batch_op:
        batch_op.add_column(
            sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("first_hit_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(op.f("ix_member_profiles_pinned"), ["pinned"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("member_profiles") as batch_op:
        batch_op.drop_index(op.f("ix_member_profiles_pinned"))
        batch_op.drop_column("first_hit_at")
        batch_op.drop_column("pinned")
