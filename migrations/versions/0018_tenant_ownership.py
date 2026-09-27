"""多租户地基：业务表 tenant_id 归属与唯一约束重审

Revision ID: 0018_tenant_ownership
Revises: 0017_tenants
Create Date: 2026-09-27

对应《多租户地基_字段级设计_v1.0.md》第 3、4、6.2 节：

1. 18 张业务表新增 ``tenant_id``（NOT NULL，外键指向 ``tenants.id``，级联删除），
   存量数据一次性回填自营租户 ``id=1``；``server_default='1'`` 保留，作为
   尚未接身份的旧写入路径的单租户兼容口径（P1-04 起由 service 层显式写入）。
2. ``web_sessions`` / ``login_history`` / ``audit_logs`` 只加可空 ``tenant_id``，
   记录归属但不参与隔离。
3. 全局唯一的"名字类"字段改按租户唯一；单列唯一索引换成非唯一索引保查询，
   ``resource_discover_tasks`` 的命名唯一约束一并换成租户内唯一。

本迁移仍不动 ``chats`` 拆表（P1-03 单独一条迁移），所以 ``routes`` 等表的
群外键暂时还指向 ``chats``。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018_tenant_ownership"
down_revision: str | None = "0017_tenants"
branch_labels: str | None = None
depends_on: str | None = None

# 业务表：带业务含义的数据，必须归属某个租户
TENANT_OWNED_TABLES = (
    "tg_accounts",
    "control_bots",
    "routes",
    "route_targets",
    "route_target_progress",
    "delivery_jobs",
    "ad_assets",
    "keyword_groups",
    "keywords",
    "leads",
    "member_profiles",
    "hot_keywords",
    "tg_resources",
    "resource_probe_logs",
    "resource_discover_tasks",
    "resource_directory_runs",
    "resource_join_tasks",
    "resource_quotas",
)

# 记录类表：只记归属，不参与隔离
OPTIONAL_TENANT_TABLES = ("web_sessions", "login_history", "audit_logs")

# 表名 → (旧单列唯一索引, 新的租户内唯一约束, 参与唯一性的列)
UNIQUE_RESCOPE: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "tg_accounts": ("ix_tg_accounts_name", "uq_tg_accounts_tenant_name", ("name",)),
    "control_bots": ("ix_control_bots_name", "uq_control_bots_tenant_name", ("name",)),
    "ad_assets": ("ix_ad_assets_name", "uq_ad_assets_tenant_name", ("name",)),
    "keyword_groups": ("ix_keyword_groups_name", "uq_keyword_groups_tenant_name", ("name",)),
    "hot_keywords": ("ix_hot_keywords_token", "uq_hot_keywords_tenant_token", ("token",)),
    "member_profiles": (
        "ix_member_profiles_tg_user_id",
        "uq_member_profiles_tenant_tg_user",
        ("tg_user_id",),
    ),
    "tg_resources": ("ix_tg_resources_tg_id", "uq_tg_resources_tenant_tg_id", ("tg_id",)),
}


def _add_tenant_ownership(table: str) -> None:
    """业务表：加 NOT NULL tenant_id（回填 1）+ 索引 + 级联外键。"""
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.create_index(batch_op.f(f"ix_{table}_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_foreign_key(
            op.f(f"fk_{table}_tenant_id_tenants"),
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="CASCADE",
        )


def _drop_tenant_ownership(table: str) -> None:
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.drop_constraint(op.f(f"fk_{table}_tenant_id_tenants"), type_="foreignkey")
        batch_op.drop_index(batch_op.f(f"ix_{table}_tenant_id"))
        batch_op.drop_column("tenant_id")


def _add_optional_tenant(table: str) -> None:
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.add_column(sa.Column("tenant_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f(f"ix_{table}_tenant_id"), ["tenant_id"], unique=False)


def _drop_optional_tenant(table: str) -> None:
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.drop_index(batch_op.f(f"ix_{table}_tenant_id"))
        batch_op.drop_column("tenant_id")


def upgrade() -> None:
    for table in TENANT_OWNED_TABLES:
        _add_tenant_ownership(table)

    for table in OPTIONAL_TENANT_TABLES:
        _add_optional_tenant(table)

    # 唯一约束按租户重审：单列唯一索引 → 非唯一索引 + 租户内复合唯一
    for table, (old_index, new_constraint, columns) in UNIQUE_RESCOPE.items():
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(op.f(old_index))
            batch_op.create_unique_constraint(op.f(new_constraint), ["tenant_id", *columns])
            batch_op.create_index(op.f(old_index), list(columns), unique=False)

    with op.batch_alter_table("resource_discover_tasks", schema=None) as batch_op:
        batch_op.drop_constraint("uq_discover_kind_keyword", type_="unique")
        batch_op.create_unique_constraint(
            op.f("uq_discover_tenant_kind_keyword"),
            ["tenant_id", "kind", "keyword"],
        )


def downgrade() -> None:
    with op.batch_alter_table("resource_discover_tasks", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("uq_discover_tenant_kind_keyword"), type_="unique")
        batch_op.create_unique_constraint(
            "uq_discover_kind_keyword",
            ["kind", "keyword"],
        )

    for table, (old_index, new_constraint, columns) in UNIQUE_RESCOPE.items():
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(op.f(old_index))
            batch_op.drop_constraint(op.f(new_constraint), type_="unique")
            batch_op.create_index(op.f(old_index), list(columns), unique=True)

    for table in reversed(OPTIONAL_TENANT_TABLES):
        _drop_optional_tenant(table)

    for table in reversed(TENANT_OWNED_TABLES):
        _drop_tenant_ownership(table)
