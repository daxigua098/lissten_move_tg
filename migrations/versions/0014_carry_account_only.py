"""A 线（搬运帖子）只能用执行账号发送

Revision ID: 0014_carry_account
Revises: 0013_sender_mode
Create Date: 2026-09-26

机器人读不到源群的帖子（Bot 隐私模式默认开启，copyMessage/forwardMessage 都报
message not found），所以 A 线的「用机器人发送」已取消；这里把历史数据里的
A 线统一改回 account。B 线（线索卡片）仍可用机器人发送。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0014_carry_account"
down_revision: str | None = "0013_sender_mode"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE routes SET sender_mode = 'account' WHERE business_type = 'A'"))


def downgrade() -> None:
    # 数据回滚没有意义（当时选了哪个机器人已无从得知），保持原样
    pass
