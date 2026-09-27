"""发信息账号：tg_accounts 增加 purpose 与运营态表

Revision ID: 0027_outreach_accounts
Revises: 0026_hot_path_indexes
Create Date: 2026-09-28

``tg_accounts`` 增加 ``purpose``（listen=执行账号 / outreach=发信息账号）与归属确认字段；
新增发信息账号的运营态（档位、状态）与按天计数两张表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0027_outreach_accounts"
down_revision: str | None = "0026_hot_path_indexes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("tg_accounts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "purpose",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'listen'"),
            )
        )
        batch_op.add_column(
            sa.Column("owner_confirmed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("owner_confirmed_by", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("owner_confirm_version", sa.String(length=16), nullable=True))
        batch_op.create_index(batch_op.f("ix_tg_accounts_purpose"), ["purpose"], unique=False)

    op.create_table(
        "outreach_account_states",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("daily_cap", sa.Integer(), nullable=True),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=True),
        sa.Column("last_cold_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("flood_wait_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("limited_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("limited_reason", sa.String(length=64), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["tg_accounts.id"],
            name=op.f("fk_outreach_account_states_account_id_tg_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_outreach_account_states_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_outreach_account_states")),
    )
    op.create_index(
        op.f("ix_outreach_account_states_tenant_id"),
        "outreach_account_states",
        ["tenant_id"],
    )
    op.create_index(
        op.f("ix_outreach_account_states_state"),
        "outreach_account_states",
        ["state"],
    )

    op.create_table(
        "outreach_account_daily",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("first_contact_sent", sa.Integer(), nullable=False),
        sa.Column("follow_up_sent", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("blocked", sa.Integer(), nullable=False),
        sa.Column("limited_hits", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["tg_accounts.id"],
            name=op.f("fk_outreach_account_daily_account_id_tg_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_outreach_account_daily_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outreach_account_daily")),
        sa.UniqueConstraint("account_id", "day", name="uq_outreach_account_daily"),
    )
    op.create_index(
        op.f("ix_outreach_account_daily_tenant_id"),
        "outreach_account_daily",
        ["tenant_id"],
    )
    op.create_index(
        op.f("ix_outreach_account_daily_account_id"),
        "outreach_account_daily",
        ["account_id"],
    )
    op.create_index(op.f("ix_outreach_account_daily_day"), "outreach_account_daily", ["day"])


def downgrade() -> None:
    op.drop_index(op.f("ix_outreach_account_daily_day"), table_name="outreach_account_daily")
    op.drop_index(
        op.f("ix_outreach_account_daily_account_id"),
        table_name="outreach_account_daily",
    )
    op.drop_index(
        op.f("ix_outreach_account_daily_tenant_id"),
        table_name="outreach_account_daily",
    )
    op.drop_table("outreach_account_daily")

    op.drop_index(op.f("ix_outreach_account_states_state"), table_name="outreach_account_states")
    op.drop_index(
        op.f("ix_outreach_account_states_tenant_id"),
        table_name="outreach_account_states",
    )
    op.drop_table("outreach_account_states")

    with op.batch_alter_table("tg_accounts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tg_accounts_purpose"))
        batch_op.drop_column("owner_confirm_version")
        batch_op.drop_column("owner_confirmed_by")
        batch_op.drop_column("owner_confirmed_at")
        batch_op.drop_column("purpose")
