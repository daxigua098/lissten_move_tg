"""为聊天对象增加备注名

Revision ID: 0006_chat_display_name
Revises: 19d5bb6bc596
Create Date: 2026-09-26

display_name 是用户自定义的备注名，优先于 Telegram 原标题显示，
用于在列表里快速识别"这是哪个群/频道"。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006_chat_display_name"
down_revision: str | None = "19d5bb6bc596"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("chats") as batch_op:
        batch_op.add_column(sa.Column("display_name", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("chats") as batch_op:
        batch_op.drop_column("display_name")
