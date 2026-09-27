"""冷触达生成队列：优先级、受阻重试与租户调度策略。

Revision ID: 0036_outreach_planning
Revises: 0035_template_media
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0036_outreach_planning"
down_revision: str | None = "0035_template_media"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("leads", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("last_plan_checked_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("last_block_reason", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("next_plan_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(
            "ix_leads_tenant_plan_ready",
            ["tenant_id", "outreach_status", "next_plan_at"],
            unique=False,
        )

    with op.batch_alter_table("outreach_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "only_authorized",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "auto_queue_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "max_lead_age_days",
                sa.Integer(),
                nullable=True,
                server_default="30",
            )
        )
        batch_op.add_column(
            sa.Column(
                "timezone",
                sa.String(length=64),
                nullable=False,
                server_default="Asia/Shanghai",
            )
        )

    with op.batch_alter_table("outreach_tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("priority", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("priority_reason", sa.String(length=255), nullable=True))
        batch_op.create_index(
            "ix_outreach_tasks_tenant_status_priority",
            ["tenant_id", "status", "priority"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("outreach_tasks", schema=None) as batch_op:
        batch_op.drop_index("ix_outreach_tasks_tenant_status_priority")
        batch_op.drop_column("priority_reason")
        batch_op.drop_column("priority")

    with op.batch_alter_table("outreach_settings", schema=None) as batch_op:
        batch_op.drop_column("timezone")
        batch_op.drop_column("max_lead_age_days")
        batch_op.drop_column("auto_queue_enabled")
        batch_op.drop_column("only_authorized")

    with op.batch_alter_table("leads", schema=None) as batch_op:
        batch_op.drop_index("ix_leads_tenant_plan_ready")
        batch_op.drop_column("next_plan_at")
        batch_op.drop_column("last_block_reason")
        batch_op.drop_column("last_plan_checked_at")
