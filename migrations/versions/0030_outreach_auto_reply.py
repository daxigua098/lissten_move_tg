"""冷触达：自动回复设置（B 模式）

Revision ID: 0030_outreach_auto_reply
Revises: 0029_outreach_messages
Create Date: 2026-09-28

P3：租户级自动回复开关与轮次上限。默认关闭，只对已回复的会话生效。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0030_outreach_auto_reply"
down_revision: str | None = "0029_outreach_messages"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("outreach_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "reply_mode",
                sa.String(length=8),
                nullable=False,
                server_default=sa.text("'human'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "auto_reply_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "auto_reply_max_rounds",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("3"),
            )
        )
        batch_op.add_column(sa.Column("auto_reply_template_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_outreach_settings_auto_reply_template_id_outreach_templates"),
            "outreach_templates",
            ["auto_reply_template_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("outreach_settings", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("fk_outreach_settings_auto_reply_template_id_outreach_templates"),
            type_="foreignkey",
        )
        batch_op.drop_column("auto_reply_template_id")
        batch_op.drop_column("auto_reply_max_rounds")
        batch_op.drop_column("auto_reply_enabled")
        batch_op.drop_column("reply_mode")
