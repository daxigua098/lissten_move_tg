"""线路支持用机器人发送

Revision ID: 0013_sender_mode
Revises: 0012_hot_keywords
Create Date: 2026-09-26

routes.sender_mode：account（默认，用执行账号发）/ bot（用选定机器人发）。
机器人只要被拉进接收群并拿到发言权限即可，风控落在机器人身上。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013_sender_mode"
down_revision: str | None = "0012_hot_keywords"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("routes") as batch_op:
        batch_op.add_column(
            sa.Column(
                "sender_mode",
                sa.String(length=16),
                nullable=False,
                server_default="account",
            )
        )
        batch_op.create_index(op.f("ix_routes_sender_mode"), ["sender_mode"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("routes") as batch_op:
        batch_op.drop_index(op.f("ix_routes_sender_mode"))
        batch_op.drop_column("sender_mode")
