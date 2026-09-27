"""到期提醒去重表（P4-05）

Revision ID: 0023_tenant_reminders
Revises: 0022_tenant_runtime
Create Date: 2026-09-27

P4-05 要求"到期前 7/3/1 天提醒代理、3/1 天提醒会员，且续期后自动消失"。
做到"只提醒一次"靠 (``tenant_id``, ``stage``, ``audience``) 唯一约束：

- 同一个租户、同一个阶段、同一个受众只落一行；
- 续期后剩余天数回到窗口外，提醒不再出现在列表里（历史记录保留，可追溯）。

``channel`` 现在固定 ``inapp``（站内），接 TG / 邮件时不用改表结构。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0023_tenant_reminders"
down_revision: str | None = "0022_tenant_runtime"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_reminders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=8), nullable=False),
        sa.Column(
            "audience",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'agent'"),
        ),
        sa.Column(
            "channel",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'inapp'"),
        ),
        sa.Column("days_left", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_tenant_reminders_tenant_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "stage",
            "audience",
            name="uq_tenant_reminder_stage",
        ),
    )
    op.create_index(
        "ix_tenant_reminders_tenant_id",
        "tenant_reminders",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_tenant_reminders_tenant_id", table_name="tenant_reminders")
    op.drop_table("tenant_reminders")
