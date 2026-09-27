"""冷触达发送记录：任务触发方式与消息快照。

Revision ID: 0037_outreach_records
Revises: 0036_outreach_planning
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0037_outreach_records"
down_revision: str | None = "0036_outreach_planning"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("outreach_tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "trigger_type",
                sa.String(length=16),
                nullable=False,
                server_default="legacy",
            )
        )
        batch_op.add_column(sa.Column("triggered_by", sa.String(length=64), nullable=True))

    with op.batch_alter_table("outreach_messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("task_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "message_kind",
                sa.String(length=16),
                nullable=False,
                server_default="legacy",
            )
        )
        batch_op.add_column(sa.Column("recipient_username", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("recipient_display_name", sa.String(length=128), nullable=True)
        )
        batch_op.add_column(sa.Column("recipient_tg_user_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("media_path", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("media_kind", sa.String(length=16), nullable=True))
        batch_op.create_index("ix_outreach_messages_task_id", ["task_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_outreach_messages_task_id",
            "outreach_tasks",
            ["task_id"],
            ["id"],
            ondelete="SET NULL",
        )

    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE outreach_messages
            SET task_id = (
                    SELECT t.id
                    FROM outreach_tasks t
                    WHERE t.contact_id = outreach_messages.contact_id
                      AND t.account_id IS outreach_messages.account_id
                      AND t.target_message_id = outreach_messages.tg_message_id
                    ORDER BY t.id
                    LIMIT 1
                ),
                message_kind = COALESCE(
                    (
                        SELECT t.kind
                        FROM outreach_tasks t
                        WHERE t.contact_id = outreach_messages.contact_id
                          AND t.account_id IS outreach_messages.account_id
                          AND t.target_message_id = outreach_messages.tg_message_id
                        ORDER BY t.id
                        LIMIT 1
                    ),
                    'legacy'
                ),
                recipient_username = (
                    SELECT c.username
                    FROM outreach_contacts c
                    WHERE c.id = outreach_messages.contact_id
                ),
                recipient_display_name = (
                    SELECT c.display_name
                    FROM outreach_contacts c
                    WHERE c.id = outreach_messages.contact_id
                ),
                recipient_tg_user_id = (
                    SELECT c.tg_user_id
                    FROM outreach_contacts c
                    WHERE c.id = outreach_messages.contact_id
                )
            WHERE outreach_messages.direction = 'out'
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("outreach_messages", schema=None) as batch_op:
        batch_op.drop_constraint("fk_outreach_messages_task_id", type_="foreignkey")
        batch_op.drop_index("ix_outreach_messages_task_id")
        batch_op.drop_column("media_kind")
        batch_op.drop_column("media_path")
        batch_op.drop_column("recipient_tg_user_id")
        batch_op.drop_column("recipient_display_name")
        batch_op.drop_column("recipient_username")
        batch_op.drop_column("message_kind")
        batch_op.drop_column("task_id")

    with op.batch_alter_table("outreach_tasks", schema=None) as batch_op:
        batch_op.drop_column("triggered_by")
        batch_op.drop_column("trigger_type")
