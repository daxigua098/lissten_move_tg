"""冷私聊抓取门槛与全局免打扰表

Revision ID: 0024_outreach_capture
Revises: 0023_tenant_reminders
Create Date: 2026-09-27

监听入库改为两阶段：关键词命中后只保存“真人 + 已有潜在可触达路径 +
未被免打扰 / 已联系 / 会话归属冲突”的用户；发送账号可以稍后再绑定。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0024_outreach_capture"
down_revision: str | None = "0023_tenant_reminders"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("leads", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "reachable_routes",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "consent_type",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'NONE'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "outreach_status",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'WAITING_SENDER_ACCOUNT'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "capture_reason",
                sa.String(length=255),
                nullable=False,
                server_default=sa.text("''"),
            )
        )
        batch_op.add_column(sa.Column("route_owner_account_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_leads_route_owner_account_id_tg_accounts"),
            "tg_accounts",
            ["route_owner_account_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            batch_op.f("ix_leads_outreach_status"),
            ["outreach_status"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_leads_route_owner_account_id"),
            ["route_owner_account_id"],
            unique=False,
        )

    with op.batch_alter_table("member_profiles", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "reachable_routes",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "consent_type",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'NONE'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "outreach_status",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'WAITING_SENDER_ACCOUNT'"),
            )
        )
        batch_op.add_column(sa.Column("conversation_owner_account_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("first_contact_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("last_contact_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_member_profiles_conversation_owner_account_id_tg_accounts"),
            "tg_accounts",
            ["conversation_owner_account_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            batch_op.f("ix_member_profiles_outreach_status"),
            ["outreach_status"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_member_profiles_conversation_owner_account_id"),
            ["conversation_owner_account_id"],
            unique=False,
        )

    op.create_table(
        "contact_suppressions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_contact_suppressions_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_suppressions")),
        sa.UniqueConstraint(
            "tenant_id",
            "tg_user_id",
            name="uq_contact_suppressions_tenant_user",
        ),
    )
    op.create_index(
        op.f("ix_contact_suppressions_tenant_id"),
        "contact_suppressions",
        ["tenant_id"],
    )
    op.create_index(
        op.f("ix_contact_suppressions_tg_user_id"),
        "contact_suppressions",
        ["tg_user_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_contact_suppressions_tg_user_id"), table_name="contact_suppressions")
    op.drop_index(op.f("ix_contact_suppressions_tenant_id"), table_name="contact_suppressions")
    op.drop_table("contact_suppressions")

    with op.batch_alter_table("member_profiles", schema=None) as batch_op:
        batch_op.drop_index(op.f("ix_member_profiles_conversation_owner_account_id"))
        batch_op.drop_index(op.f("ix_member_profiles_outreach_status"))
        batch_op.drop_constraint(
            op.f("fk_member_profiles_conversation_owner_account_id_tg_accounts"),
            type_="foreignkey",
        )
        batch_op.drop_column("last_contact_at")
        batch_op.drop_column("first_contact_at")
        batch_op.drop_column("conversation_owner_account_id")
        batch_op.drop_column("outreach_status")
        batch_op.drop_column("consent_type")
        batch_op.drop_column("reachable_routes")

    with op.batch_alter_table("leads", schema=None) as batch_op:
        batch_op.drop_index(op.f("ix_leads_route_owner_account_id"))
        batch_op.drop_index(op.f("ix_leads_outreach_status"))
        batch_op.drop_constraint(
            op.f("fk_leads_route_owner_account_id_tg_accounts"),
            type_="foreignkey",
        )
        batch_op.drop_column("route_owner_account_id")
        batch_op.drop_column("capture_reason")
        batch_op.drop_column("outreach_status")
        batch_op.drop_column("consent_type")
        batch_op.drop_column("reachable_routes")
