"""发信息账号：接码地址字段

Revision ID: 0033_account_code_url
Revises: 0032_outreach_metrics
Create Date: 2026-09-28

发信息账号可以直接用「手机号 + 接码地址」登记：地址里的 token 属凭据，加密存储。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0033_account_code_url"
down_revision: str | None = "0032_outreach_metrics"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("tg_accounts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("code_url_enc", sa.String(length=512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tg_accounts", schema=None) as batch_op:
        batch_op.drop_column("code_url_enc")
