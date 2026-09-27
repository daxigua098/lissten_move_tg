"""冷触达 P5：7 日指标、账号退役字段与冷触达额度

Revision ID: 0032_outreach_metrics
Revises: 0031_outreach_handoff
Create Date: 2026-09-28

- ``outreach_account_states``：近 7 天成功率 / 回复率 / 在跟会话数；
- ``tg_accounts``：退役（软删）时间与原因；
- ``tenant_limits``：发信息账号数上限与每日冷聊总量上限。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0032_outreach_metrics"
down_revision: str | None = "0031_outreach_handoff"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("outreach_account_states", schema=None) as batch_op:
        batch_op.add_column(sa.Column("success_rate_7d", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("reply_rate_7d", sa.Float(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "active_conversation_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(sa.Column("metrics_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("tg_accounts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("retire_reason", sa.String(length=64), nullable=True))

    with op.batch_alter_table("tenant_limits", schema=None) as batch_op:
        batch_op.add_column(sa.Column("max_outreach_accounts", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("max_daily_cold_outreach", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tenant_limits", schema=None) as batch_op:
        batch_op.drop_column("max_daily_cold_outreach")
        batch_op.drop_column("max_outreach_accounts")

    with op.batch_alter_table("tg_accounts", schema=None) as batch_op:
        batch_op.drop_column("retire_reason")
        batch_op.drop_column("retired_at")

    with op.batch_alter_table("outreach_account_states", schema=None) as batch_op:
        batch_op.drop_column("metrics_at")
        batch_op.drop_column("active_conversation_count")
        batch_op.drop_column("reply_rate_7d")
        batch_op.drop_column("success_rate_7d")
