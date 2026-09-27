"""冷触达会话消息流水

Revision ID: 0029_outreach_messages
Revises: 0028_outreach_queue
Create Date: 2026-09-28

P2：记录出站发送与入站回复，用于会话画像、归属锁定与审计。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0029_outreach_messages"
down_revision: str | None = "0028_outreach_queue"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "outreach_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("bot_id", sa.Integer(), nullable=True),
        sa.Column("direction", sa.String(length=4), nullable=False),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("generated_by", sa.String(length=8), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_outreach_messages_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["outreach_contacts.id"],
            name=op.f("fk_outreach_messages_contact_id_outreach_contacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["tg_accounts.id"],
            name=op.f("fk_outreach_messages_account_id_tg_accounts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["bot_id"],
            ["control_bots.id"],
            name=op.f("fk_outreach_messages_bot_id_control_bots"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outreach_messages")),
        sa.UniqueConstraint(
            "account_id",
            "direction",
            "tg_message_id",
            name="uq_outreach_messages_tg",
        ),
    )
    op.create_index(op.f("ix_outreach_messages_tenant_id"), "outreach_messages", ["tenant_id"])
    op.create_index(op.f("ix_outreach_messages_contact_id"), "outreach_messages", ["contact_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_outreach_messages_contact_id"), table_name="outreach_messages")
    op.drop_index(op.f("ix_outreach_messages_tenant_id"), table_name="outreach_messages")
    op.drop_table("outreach_messages")
