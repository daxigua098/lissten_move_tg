"""线路支持多监听源：新增 bundle_id 把同一组线路串起来

Revision ID: 0008_route_bundle
Revises: 0007_leads_keywords
Create Date: 2026-09-26

一条「线路」允许多选监听源。底层的投递水位线是按「线路 + 接收目标」记的，
所以落库仍然一个源一行；同一组共享 bundle_id，界面上当成一条线路编辑。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008_route_bundle"
down_revision: str | None = "0007_leads_keywords"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("routes") as batch_op:
        batch_op.add_column(sa.Column("bundle_id", sa.String(length=32), nullable=True))
        batch_op.create_index(op.f("ix_routes_bundle_id"), ["bundle_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("routes") as batch_op:
        batch_op.drop_index(op.f("ix_routes_bundle_id"))
        batch_op.drop_column("bundle_id")
