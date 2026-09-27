"""到期强停：tenants 补运行开关与停用/过期痕迹（P4-01 / P4-03）

Revision ID: 0022_tenant_runtime
Revises: 0021_agent_quotas
Create Date: 2026-09-27

对应《到期强停_字段级设计_v1.0.md》第 1 节：

- ``expired_at``：首次判定为过期的时刻（追溯用，幂等靠 P3 的 ``quota_held``）；
- ``suspended_at`` / ``suspended_by`` / ``suspended_reason``：停用痕迹；
- ``runtime_enabled``：**租户运行总开关**，默认关闭——新开的号必须登录后手动启动；
- ``runtime_stop_reason``：最近一次停止的原因（``manual`` / ``expired`` / ``suspended``）。

存量数据：自营租户（``id=1``）是平台自己在跑的业务，迁移时直接把开关打开，
否则升级完它自己的线路会被"新号默认关闭"的口径一起停掉。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0022_tenant_runtime"
down_revision: str | None = "0021_agent_quotas"
branch_labels: str | None = None
depends_on: str | None = None

_ADDED_COLUMNS = (
    "expired_at",
    "suspended_at",
    "suspended_by",
    "suspended_reason",
    "runtime_enabled",
    "runtime_stop_reason",
)


def upgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.add_column(sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("suspended_by", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("suspended_reason", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column(
                "runtime_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(sa.Column("runtime_stop_reason", sa.String(length=24), nullable=True))

    # 自营租户永不过期、且必须保持运行；会员租户一律保持"待手动启动"
    op.execute("UPDATE tenants SET runtime_enabled = 1 WHERE id = 1")


def downgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        for column in reversed(_ADDED_COLUMNS):
            batch_op.drop_column(column)
