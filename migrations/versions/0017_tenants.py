"""多租户地基：tenants 表与 users 归属字段

Revision ID: 0017_tenants
Revises: 0016_directory_channels
Create Date: 2026-09-27

对应《多租户地基_字段级设计_v1.0.md》第 1、2、6.2 节：

- 新建 ``tenants``，并插入自营租户 ``id=1``（``kind='self'``，不过期）；
  存量业务数据在后续迁移里全部回填到它下面。
- ``users`` 增 ``account_type``（存量回填 ``platform``）与 ``tenant_id``（为空）。

本迁移只做加法：不动业务表、不拆 ``chats``。``tenant_id`` 下沉到业务表、
唯一约束按租户重审、``chats`` 拆表分别在后续迁移里完成。

``account_type`` 是非空新列，给一个 server_default 让存量账号落在 ``platform``
（模型侧只声明 Python 默认值，不产生结构差异）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017_tenants"
down_revision: str | None = "0016_directory_channels"
branch_labels: str | None = None
depends_on: str | None = None

# 自营租户固定主键：与 app.db.models.SELF_TENANT_ID 保持一致
SELF_TENANT_ID = 1


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind <> 'member' OR owner_user_id IS NOT NULL",
            name=op.f("ck_tenants_member_owner_required"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_tenants_owner_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("name", name=op.f("uq_tenants_name")),
        sa.UniqueConstraint("owner_user_id", name=op.f("uq_tenants_owner_user_id")),
    )
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_tenants_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_tenants_expires_at"), ["expires_at"], unique=False)

    # 自营租户：存量数据后续全部挂到它下面，永不过期
    op.execute(
        "INSERT INTO tenants "
        "(id, name, kind, status, expires_at, owner_user_id, created_by, note, "
        " created_at, updated_at) "
        f"VALUES ({SELF_TENANT_ID}, '自营', 'self', 'active', NULL, NULL, 'migration', "
        " NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "account_type",
                sa.String(length=16),
                nullable=False,
                server_default="platform",
            )
        )
        batch_op.add_column(sa.Column("tenant_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_users_account_type"), ["account_type"], unique=False)
        batch_op.create_index(batch_op.f("ix_users_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_foreign_key(
            op.f("fk_users_tenant_id_tenants"),
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("fk_users_tenant_id_tenants"), type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_users_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_users_account_type"))
        batch_op.drop_column("tenant_id")
        batch_op.drop_column("account_type")

    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tenants_expires_at"))
        batch_op.drop_index(batch_op.f("ix_tenants_status"))

    op.drop_table("tenants")
