"""「常规开通」功能包改成全功能

Revision ID: 0025_standard_plan_full
Revises: 0024_outreach_capture
Create Date: 2026-09-27

预置模板 ``standard``（常规开通）原来是 ``modules = []``，选它开会员会开出一个
只有「账号与机器人」的空白账号。按最终口径「只要开通了会员，就能用全部实用功能
（代理系统除外）」，把它的功能块补成 搬运帖子 / 监听会员 / 资源发现。

只更新仍然为空的那条，避免覆盖运营手工改过的模板。
"""

from __future__ import annotations

from alembic import op

revision: str = "0025_standard_plan_full"
down_revision: str | None = "0024_outreach_capture"
branch_labels: str | None = None
depends_on: str | None = None

FULL_MODULES = '["carry", "monitor", "discovery"]'


def upgrade() -> None:
    op.execute(
        "UPDATE plan_templates SET modules = '"
        + FULL_MODULES
        + "', updated_at = CURRENT_TIMESTAMP "
        + "WHERE code = 'standard' AND (modules IS NULL OR trim(modules) IN ('', '[]'))"
    )


def downgrade() -> None:
    op.execute("UPDATE plan_templates SET modules = '[]' WHERE code = 'standard'")
