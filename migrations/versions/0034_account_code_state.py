"""发信息账号：取码结果与冷却字段

Revision ID: 0034_account_code_state
Revises: 0033_account_code_url
Create Date: 2026-09-28

- 接码平台一返回就保存验证码 / 二级密码（登录失败也能看到）；
- 记录接码平台「30 分钟内没有新验证码」的冷却截止时间，冷却期内自动跳过。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0034_account_code_state"
down_revision: str | None = "0033_account_code_url"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("tg_accounts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_code_enc", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("last_code_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_2fa_enc", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column("code_cooldown_until", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("tg_accounts", schema=None) as batch_op:
        batch_op.drop_column("code_cooldown_until")
        batch_op.drop_column("last_2fa_enc")
        batch_op.drop_column("last_code_at")
        batch_op.drop_column("last_code_enc")
