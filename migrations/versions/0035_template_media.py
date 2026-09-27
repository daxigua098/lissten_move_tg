"""冷触达话术模板：支持富媒体（跟进 / 自动回复）

Revision ID: 0035_template_media
Revises: 0034_account_code_state
Create Date: 2026-09-28

跟进 / 自动回复模板可以带一张图片或一个视频（存 assets/uploads）；
首条招呼仍然禁止链接与媒体（反垃圾红线）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0035_template_media"
down_revision: str | None = "0034_account_code_state"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("outreach_templates", schema=None) as batch_op:
        batch_op.add_column(sa.Column("media_path", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("media_kind", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("outreach_templates", schema=None) as batch_op:
        batch_op.drop_column("media_kind")
        batch_op.drop_column("media_path")
