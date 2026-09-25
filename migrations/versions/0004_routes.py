"""初始化线路与广告素材表

Revision ID: 0004_routes
Revises: 0003_chat_roles
Create Date: 2026-09-25

创建 ad_assets（广告素材）、routes（线路）、route_targets（线路目标）、
route_target_progress（目标级水位线）。
A 线 / B 线的规则以 JSON 存在 routes.a_config / b_config，由
app.core.route_config 校验与补默认值。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_routes"
down_revision: str | None = "0003_chat_roles"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "ad_assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("image_path", sa.String(length=255), nullable=True),
        sa.Column("link_url", sa.String(length=512), nullable=True),
        sa.Column("link_text", sa.String(length=64), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ad_assets")),
    )
    op.create_index("ix_ad_assets_name", "ad_assets", ["name"], unique=True)

    op.create_table(
        "routes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("source_chat_id", sa.Integer(), nullable=False),
        sa.Column("business_type", sa.String(length=2), nullable=False),
        sa.Column("exec_account_id", sa.Integer(), nullable=True),
        sa.Column("notify_bot_id", sa.Integer(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("delay_seconds", sa.Float(), nullable=False),
        sa.Column("hourly_limit", sa.Integer(), nullable=True),
        sa.Column("daily_limit", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("a_config", sa.Text(), nullable=False),
        sa.Column("b_config", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["exec_account_id"],
            ["tg_accounts.id"],
            name=op.f("fk_routes_exec_account_id_tg_accounts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["notify_bot_id"],
            ["control_bots.id"],
            name=op.f("fk_routes_notify_bot_id_control_bots"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_chat_id"],
            ["chats.id"],
            name=op.f("fk_routes_source_chat_id_chats"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routes")),
    )
    op.create_index("ix_routes_name", "routes", ["name"], unique=False)
    op.create_index("ix_routes_business_type", "routes", ["business_type"], unique=False)
    op.create_index("ix_routes_source_chat_id", "routes", ["source_chat_id"], unique=False)
    op.create_index("ix_routes_enabled", "routes", ["enabled"], unique=False)

    op.create_table(
        "route_targets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("route_id", sa.Integer(), nullable=False),
        sa.Column("target_chat_id", sa.Integer(), nullable=False),
        sa.Column("target_role", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["route_id"],
            ["routes.id"],
            name=op.f("fk_route_targets_route_id_routes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_chat_id"],
            ["chats.id"],
            name=op.f("fk_route_targets_target_chat_id_chats"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_route_targets")),
        sa.UniqueConstraint("route_id", "target_chat_id", name="uq_route_target"),
    )
    op.create_index("ix_route_targets_route_id", "route_targets", ["route_id"], unique=False)
    op.create_index(
        "ix_route_targets_target_chat_id",
        "route_targets",
        ["target_chat_id"],
        unique=False,
    )

    op.create_table(
        "route_target_progress",
        sa.Column("route_id", sa.Integer(), nullable=False),
        sa.Column("target_chat_id", sa.Integer(), nullable=False),
        sa.Column("last_delivered_message_id", sa.BigInteger(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("backfill_status", sa.String(length=16), nullable=False),
        sa.Column("backfill_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["route_id"],
            ["routes.id"],
            name=op.f("fk_route_target_progress_route_id_routes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_chat_id"],
            ["chats.id"],
            name=op.f("fk_route_target_progress_target_chat_id_chats"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "route_id",
            "target_chat_id",
            name=op.f("pk_route_target_progress"),
        ),
    )


def downgrade() -> None:
    op.drop_table("route_target_progress")

    op.drop_index("ix_route_targets_target_chat_id", table_name="route_targets")
    op.drop_index("ix_route_targets_route_id", table_name="route_targets")
    op.drop_table("route_targets")

    op.drop_index("ix_routes_enabled", table_name="routes")
    op.drop_index("ix_routes_source_chat_id", table_name="routes")
    op.drop_index("ix_routes_business_type", table_name="routes")
    op.drop_index("ix_routes_name", table_name="routes")
    op.drop_table("routes")

    op.drop_index("ix_ad_assets_name", table_name="ad_assets")
    op.drop_table("ad_assets")
