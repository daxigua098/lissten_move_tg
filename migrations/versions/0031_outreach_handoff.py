"""冷触达：Bot 转交令牌

Revision ID: 0031_outreach_handoff
Revises: 0030_outreach_auto_reply
Create Date: 2026-09-28

P4：用户点 Bot 的 /start 后，会话归属从普通账号转给 Bot。
只在「用户已回复」的会话里可用；首条冷消息禁止放链接。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0031_outreach_handoff"
down_revision: str | None = "0030_outreach_auto_reply"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("outreach_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "handoff_bot_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(sa.Column("handoff_bot_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_outreach_settings_handoff_bot_id_control_bots"),
            "control_bots",
            ["handoff_bot_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "outreach_handoff_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("bot_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_outreach_handoff_tokens_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["outreach_contacts.id"],
            name=op.f("fk_outreach_handoff_tokens_contact_id_outreach_contacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["bot_id"],
            ["control_bots.id"],
            name=op.f("fk_outreach_handoff_tokens_bot_id_control_bots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outreach_handoff_tokens")),
        sa.UniqueConstraint("token", name="uq_outreach_handoff_tokens_token"),
    )
    op.create_index(
        op.f("ix_outreach_handoff_tokens_tenant_id"),
        "outreach_handoff_tokens",
        ["tenant_id"],
    )
    op.create_index(
        op.f("ix_outreach_handoff_tokens_contact_id"),
        "outreach_handoff_tokens",
        ["contact_id"],
    )
    op.create_index(
        op.f("ix_outreach_handoff_tokens_bot_id"),
        "outreach_handoff_tokens",
        ["bot_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_outreach_handoff_tokens_bot_id"), table_name="outreach_handoff_tokens")
    op.drop_index(
        op.f("ix_outreach_handoff_tokens_contact_id"),
        table_name="outreach_handoff_tokens",
    )
    op.drop_index(
        op.f("ix_outreach_handoff_tokens_tenant_id"),
        table_name="outreach_handoff_tokens",
    )
    op.drop_table("outreach_handoff_tokens")

    with op.batch_alter_table("outreach_settings", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("fk_outreach_settings_handoff_bot_id_control_bots"),
            type_="foreignkey",
        )
        batch_op.drop_column("handoff_bot_id")
        batch_op.drop_column("handoff_bot_enabled")
