"""账号体系：功能包模板、租户功能授权、用量限制、代理上级字段

Revision ID: 0020_account_modules
Revises: 0019_chat_directory
Create Date: 2026-09-27

对应《账号体系与权限_字段级设计_v1.0.md》第 2、3 节：

- ``plan_templates``：功能包模板（开号时的快捷选项，**不是**运行时授权依据），
  并预置 ``trial_carry`` / ``trial_monitor`` / ``standard`` / ``full`` 四条。
- ``tenant_modules``：租户功能授权，运行时判断依据，主键 ``(tenant_id, module)``。
- ``tenant_limits``：租户用量限制（目前只服务试用），主键 ``tenant_id``。
- ``users.parent_user_id``：代理/会员的直属上级，自引用 FK，不限层级。

功能块固定三个：``carry`` / ``monitor`` / ``discovery``。基础能力（登录、改密、
TG 账号、机器人、运行总览）对所有会员恒开，**不落库**。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0020_account_modules"
down_revision: str | None = "0019_chat_directory"
branch_labels: str | None = None
depends_on: str | None = None

# 预置功能包模板：(code, name, kind, modules, limits)
PLAN_SEEDS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "trial_carry",
        "1 天试用（搬运）",
        "trial",
        '["carry"]',
        '{"max_routes": 1, "allow_export": false}',
    ),
    (
        "trial_monitor",
        "1 天试用（监听）",
        "trial",
        '["monitor"]',
        '{"max_routes": 1, "allow_export": false}',
    ),
    ("standard", "常规开通", "standard", "[]", "{}"),
    ("full", "全功能", "standard", '["carry", "monitor", "discovery"]', "{}"),
)


def upgrade() -> None:
    op.create_table(
        "plan_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("modules", sa.Text(), nullable=False),
        sa.Column("limits", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_templates")),
    )
    with op.batch_alter_table("plan_templates", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_plan_templates_code"), ["code"], unique=True)
        batch_op.create_index(batch_op.f("ix_plan_templates_kind"), ["kind"], unique=False)
        batch_op.create_index(batch_op.f("ix_plan_templates_enabled"), ["enabled"], unique=False)

    for code, name, kind, modules, limits in PLAN_SEEDS:
        op.execute(
            "INSERT INTO plan_templates "
            "(code, name, kind, modules, limits, enabled, created_at, updated_at) "
            f"VALUES ('{code}', '{name}', '{kind}', '{modules}', '{limits}', 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )

    op.create_table(
        "tenant_modules",
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("module", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("granted_by", sa.String(length=64), nullable=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_modules_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "module", name=op.f("pk_tenant_modules")),
    )

    op.create_table(
        "tenant_limits",
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("max_routes", sa.Integer(), nullable=True),
        sa.Column("max_tg_accounts", sa.Integer(), nullable=True),
        sa.Column("max_sources", sa.Integer(), nullable=True),
        sa.Column("allow_export", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_limits_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_tenant_limits")),
    )

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("parent_user_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_users_parent_user_id"), ["parent_user_id"], unique=False
        )
        batch_op.create_foreign_key(
            op.f("fk_users_parent_user_id_users"),
            "users",
            ["parent_user_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("fk_users_parent_user_id_users"), type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_users_parent_user_id"))
        batch_op.drop_column("parent_user_id")

    op.drop_table("tenant_limits")
    op.drop_table("tenant_modules")
    op.drop_table("plan_templates")
