"""多租户地基：chats 拆成 chat_directory + tenant_chats

Revision ID: 0019_chat_directory
Revises: 0018_tenant_ownership
Create Date: 2026-09-27

对应《多租户地基_字段级设计_v1.0.md》第 5、6.3 节：

- ``chat_directory``：群/频道的客观信息（tg_id / 标题 / 类型 / 成员数），全平台一份；
- ``tenant_chats``：租户 × 群关系（备注名、标签、源/接收组、启停），按租户一份。

**ID 沿用技巧**：租户侧 ``tenant_chats.id`` 直接沿用原 ``chats.id``，目录侧也用同一个
id（迁移时一个群只对应一个租户，是 1:1）。这样 ``routes.source_chat_id``、
``route_targets.target_chat_id``、``route_target_progress.target_chat_id``、
``leads.source_chat_id`` 的**值一个都不用改**，只换外键指向的表。

第二个租户开始配置同一个群时，``chat_directory`` 复用已有行、``tenant_chats``
新增一行并分配新 id，两张表才真正分叉。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019_chat_directory"
down_revision: str | None = "0018_tenant_ownership"
branch_labels: str | None = None
depends_on: str | None = None

# 指向 chats 的外键：表名 → ((列名, 删除行为), ...)
CHAT_FOREIGN_KEYS: dict[str, tuple[tuple[str, str], ...]] = {
    "routes": (("source_chat_id", "CASCADE"),),
    "route_targets": (("target_chat_id", "CASCADE"),),
    "route_target_progress": (("target_chat_id", "CASCADE"),),
    "leads": (("source_chat_id", "SET NULL"),),
}

COPY_DIRECTORY_SQL = """
INSERT INTO chat_directory
    (id, tg_id, chat_type, title, username, is_private, member_count, source_kind,
     created_at, updated_at)
SELECT id, tg_id, chat_type, title, username, is_private, member_count, source_kind,
       created_at, updated_at
FROM chats
"""

COPY_TENANT_CHATS_SQL = """
INSERT INTO tenant_chats
    (id, tenant_id, chat_id, display_name, tags, is_source, source_enabled, is_target,
     target_enabled, target_role, joined, can_post, note, created_at, updated_at)
SELECT id, 1, id, display_name, tags, is_source, source_enabled, is_target,
       target_enabled, target_role, joined, can_post, note, created_at, updated_at
FROM chats
"""

RESTORE_CHATS_SQL = """
INSERT INTO chats
    (id, tg_id, chat_type, title, display_name, username, is_private, joined, can_post,
     member_count, tags, source_kind, is_source, source_enabled, is_target,
     target_enabled, target_role, note, created_at, updated_at)
SELECT tc.id, cd.tg_id, cd.chat_type, cd.title, tc.display_name, cd.username,
       cd.is_private, tc.joined, tc.can_post, cd.member_count, tc.tags, cd.source_kind,
       tc.is_source, tc.source_enabled, tc.is_target, tc.target_enabled,
       tc.target_role, tc.note, tc.created_at, tc.updated_at
FROM tenant_chats AS tc
JOIN chat_directory AS cd ON cd.id = tc.chat_id
WHERE tc.tenant_id = 1
"""


def _repoint_chat_foreign_keys(*, source: str, target: str) -> None:
    """把群外键从 `source` 表改指到 `target` 表（列值不变，只换目标）。"""
    for table, columns in CHAT_FOREIGN_KEYS.items():
        for column, ondelete in columns:
            with op.batch_alter_table(table, schema=None) as batch_op:
                batch_op.drop_constraint(
                    op.f(f"fk_{table}_{column}_{source}"),
                    type_="foreignkey",
                )
                batch_op.create_foreign_key(
                    op.f(f"fk_{table}_{column}_{target}"),
                    target,
                    [column],
                    ["id"],
                    ondelete=ondelete,
                )


def upgrade() -> None:
    op.create_table(
        "chat_directory",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_type", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("is_private", sa.Boolean(), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=True),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_directory")),
    )
    with op.batch_alter_table("chat_directory", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_chat_directory_tg_id"), ["tg_id"], unique=True)
        batch_op.create_index(
            batch_op.f("ix_chat_directory_chat_type"), ["chat_type"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_chat_directory_source_kind"), ["source_kind"], unique=False
        )

    op.create_table(
        "tenant_chats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=True),
        sa.Column("tags", sa.Text(), nullable=False),
        sa.Column("is_source", sa.Boolean(), nullable=False),
        sa.Column("source_enabled", sa.Boolean(), nullable=False),
        sa.Column("is_target", sa.Boolean(), nullable=False),
        sa.Column("target_enabled", sa.Boolean(), nullable=False),
        sa.Column("target_role", sa.String(length=16), nullable=False),
        sa.Column("joined", sa.Boolean(), nullable=False),
        sa.Column("can_post", sa.Boolean(), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["chat_directory.id"],
            name=op.f("fk_tenant_chats_chat_id_chat_directory"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_chats_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_chats")),
        sa.UniqueConstraint("tenant_id", "chat_id", name=op.f("uq_tenant_chats_tenant_chat")),
    )
    with op.batch_alter_table("tenant_chats", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_tenant_chats_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tenant_chats_chat_id"), ["chat_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tenant_chats_is_source"), ["is_source"], unique=False)
        batch_op.create_index(batch_op.f("ix_tenant_chats_is_target"), ["is_target"], unique=False)

    # 1) 客观信息进目录（id 沿用原 chats.id）
    op.execute(COPY_DIRECTORY_SQL)
    # 2) 主观信息挂到自营租户（id 沿用原 chats.id）
    op.execute(COPY_TENANT_CHATS_SQL)

    # 3) 外键改指 tenant_chats：值不变，只换目标表
    _repoint_chat_foreign_keys(source="chats", target="tenant_chats")

    # 4) 旧表退役
    op.drop_table("chats")


def downgrade() -> None:
    op.create_table(
        "chats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_type", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("is_private", sa.Boolean(), nullable=False),
        sa.Column("joined", sa.Boolean(), nullable=False),
        sa.Column("can_post", sa.Boolean(), nullable=True),
        sa.Column("member_count", sa.Integer(), nullable=True),
        sa.Column("tags", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_source", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("source_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_target", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("target_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "target_role",
            sa.String(length=16),
            nullable=False,
            server_default="content",
        ),
        sa.Column("display_name", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chats")),
    )
    with op.batch_alter_table("chats", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_chats_tg_id"), ["tg_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_chats_chat_type"), ["chat_type"], unique=False)
        batch_op.create_index(batch_op.f("ix_chats_source_kind"), ["source_kind"], unique=False)
        batch_op.create_index(batch_op.f("ix_chats_is_source"), ["is_source"], unique=False)
        batch_op.create_index(batch_op.f("ix_chats_is_target"), ["is_target"], unique=False)

    # 只有自营租户的数据能还原回单表结构（多租户数据本就无法压回 chats）
    op.execute(RESTORE_CHATS_SQL)

    _repoint_chat_foreign_keys(source="tenant_chats", target="chats")

    op.drop_table("tenant_chats")
    op.drop_table("chat_directory")
