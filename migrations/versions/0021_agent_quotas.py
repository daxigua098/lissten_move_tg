"""额度与代理：agent_quotas + quota_ledger + tenants 额度归属三列

Revision ID: 0021_agent_quotas
Revises: 0020_account_modules
Create Date: 2026-09-27

对应《额度与代理_字段级设计_v1.0.md》第 2、3、4 节：

- ``agent_quotas``：代理三类余额（会员 / 代理 / 试用），一个代理一行，均不允许为负；
- ``quota_ledger``：额度流水，一次划拨写两行并共享 ``transfer_id``；
- ``tenants`` 补 ``owner_agent_id`` / ``quota_type`` / ``quota_held``，
  用于"到期释放回哪个代理"与"释放只能执行一次"的幂等。

存量租户（含自营）一律 ``quota_type='none'``、``quota_held=false``：它们不是代理开出来的。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0021_agent_quotas"
down_revision: str | None = "0020_account_modules"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "agent_quotas",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("member_quota", sa.Integer(), nullable=False),
        sa.Column("agent_quota", sa.Integer(), nullable=False),
        sa.Column("trial_quota", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "member_quota >= 0", name=op.f("ck_agent_quotas_member_quota_non_negative")
        ),
        sa.CheckConstraint(
            "agent_quota >= 0", name=op.f("ck_agent_quotas_agent_quota_non_negative")
        ),
        sa.CheckConstraint(
            "trial_quota >= 0", name=op.f("ck_agent_quotas_trial_quota_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_agent_quotas_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_quotas")),
    )
    with op.batch_alter_table("agent_quotas", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_agent_quotas_user_id"), ["user_id"], unique=True)

    op.create_table(
        "quota_ledger",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transfer_id", sa.String(length=32), nullable=True),
        sa.Column("subject_user_id", sa.Integer(), nullable=True),
        sa.Column("subject_username", sa.String(length=64), nullable=False),
        sa.Column("quota_type", sa.String(length=8), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("change", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("related_tenant_id", sa.Integer(), nullable=True),
        sa.Column("related_user_id", sa.Integer(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("actor_username", sa.String(length=64), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("change <> 0", name=op.f("ck_quota_ledger_change_non_zero")),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_quota_ledger_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["related_tenant_id"],
            ["tenants.id"],
            name=op.f("fk_quota_ledger_related_tenant_id_tenants"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["related_user_id"],
            ["users.id"],
            name=op.f("fk_quota_ledger_related_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["subject_user_id"],
            ["users.id"],
            name=op.f("fk_quota_ledger_subject_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quota_ledger")),
    )
    with op.batch_alter_table("quota_ledger", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_quota_ledger_transfer_id"), ["transfer_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_quota_ledger_related_tenant_id"), ["related_tenant_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_quota_ledger_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_quota_ledger_quota_type"), ["quota_type"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_quota_ledger_action"), ["action"], unique=False)
        batch_op.create_index(
            "ix_quota_ledger_subject_created",
            ["subject_user_id", "created_at"],
            unique=False,
        )

    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_agent_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "quota_type",
                sa.String(length=8),
                nullable=False,
                server_default="none",
            )
        )
        batch_op.add_column(
            sa.Column(
                "quota_held",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.create_index(
            batch_op.f("ix_tenants_owner_agent_id"), ["owner_agent_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_tenants_quota_type"), ["quota_type"], unique=False)
        batch_op.create_foreign_key(
            op.f("fk_tenants_owner_agent_id_users"),
            "users",
            ["owner_agent_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("fk_tenants_owner_agent_id_users"), type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_tenants_quota_type"))
        batch_op.drop_index(batch_op.f("ix_tenants_owner_agent_id"))
        batch_op.drop_column("quota_held")
        batch_op.drop_column("quota_type")
        batch_op.drop_column("owner_agent_id")

    op.drop_table("quota_ledger")
    op.drop_table("agent_quotas")
