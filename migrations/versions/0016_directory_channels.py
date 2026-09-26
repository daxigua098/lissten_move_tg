"""三方目录站渠道：资源增量字段、发现任务渠道与目录同步记录

Revision ID: 0016_directory_channels
Revises: 0015_resource_discovery
Create Date: 2026-09-26

对应《资源发现模块 需求说明书 v1.2》第 5 章：

- ``tg_resources`` 增 6 列：来源站点、目录名次 / 成员数 / 同步时间、内容分级、头像路径；
- ``resource_discover_tasks`` 增 ``source``（combot / tgme / telegram）；
- 新表 ``resource_directory_runs``：目录同步历史，兼作断点续抓的续点。

``content_rating`` 是新增的非空列，给一个 server_default 让老数据落在
``unknown``（模型侧只声明 Python 默认值，不产生结构差异）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016_directory_channels"
down_revision: str | None = "0015_resource_discovery"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("tg_resources", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source_site", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("directory_rank", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("directory_member_count", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("directory_synced_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "content_rating",
                sa.String(length=16),
                nullable=False,
                server_default="unknown",
            )
        )
        batch_op.add_column(sa.Column("avatar_path", sa.String(length=255), nullable=True))
        batch_op.create_index(op.f("ix_tg_resources_source_site"), ["source_site"], unique=False)
        batch_op.create_index(
            op.f("ix_tg_resources_content_rating"), ["content_rating"], unique=False
        )

    with op.batch_alter_table("resource_discover_tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source", sa.String(length=16), nullable=True))
        batch_op.create_index(op.f("ix_resource_discover_tasks_source"), ["source"], unique=False)

    op.create_table(
        "resource_directory_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pages_done", sa.Integer(), nullable=False),
        sa.Column("pages_total", sa.Integer(), nullable=True),
        sa.Column("items_seen", sa.Integer(), nullable=False),
        sa.Column("items_added", sa.Integer(), nullable=False),
        sa.Column("requests_used", sa.Integer(), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resource_directory_runs")),
    )
    with op.batch_alter_table("resource_directory_runs", schema=None) as batch_op:
        batch_op.create_index(op.f("ix_resource_directory_runs_source"), ["source"], unique=False)
        batch_op.create_index(op.f("ix_resource_directory_runs_scope"), ["scope"], unique=False)
        batch_op.create_index(
            op.f("ix_resource_directory_runs_started_at"), ["started_at"], unique=False
        )
        batch_op.create_index(op.f("ix_resource_directory_runs_result"), ["result"], unique=False)
        batch_op.create_index(
            "ix_resource_directory_source_scope",
            ["source", "scope"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("resource_directory_runs")

    with op.batch_alter_table("resource_discover_tasks", schema=None) as batch_op:
        batch_op.drop_index(op.f("ix_resource_discover_tasks_source"))
        batch_op.drop_column("source")

    with op.batch_alter_table("tg_resources", schema=None) as batch_op:
        batch_op.drop_index(op.f("ix_tg_resources_content_rating"))
        batch_op.drop_index(op.f("ix_tg_resources_source_site"))
        batch_op.drop_column("avatar_path")
        batch_op.drop_column("content_rating")
        batch_op.drop_column("directory_synced_at")
        batch_op.drop_column("directory_member_count")
        batch_op.drop_column("directory_rank")
        batch_op.drop_column("source_site")
