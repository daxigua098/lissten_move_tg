"""资源发现模块：资源库、探测日志、发现任务、加群队列与配额

Revision ID: 0015_resource_discovery
Revises: 0014_carry_account
Create Date: 2026-09-26

对应《资源发现模块 需求说明书 v1.1》第 5 章。资源库与探测日志长期保留，
不参与线上线索的 3 天清理。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0015_resource_discovery"
down_revision: str | None = "0014_carry_account"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "tg_resources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tg_id", sa.BigInteger(), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("invite_link", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("title_history", sa.Text(), nullable=False),
        sa.Column("about", sa.Text(), nullable=True),
        sa.Column("chat_type", sa.String(length=16), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=True),
        sa.Column("member_count_approx", sa.Boolean(), nullable=False),
        sa.Column("member_count_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posts_per_day", sa.Float(), nullable=True),
        sa.Column("human_ratio", sa.Float(), nullable=True),
        sa.Column("unique_senders", sa.Integer(), nullable=True),
        sa.Column("link_density", sa.Float(), nullable=True),
        sa.Column("activity_score", sa.Float(), nullable=True),
        sa.Column("lead_potential", sa.Float(), nullable=True),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("country", sa.String(length=8), nullable=True),
        sa.Column("categories", sa.Text(), nullable=False),
        sa.Column("is_index_group", sa.Boolean(), nullable=False),
        sa.Column("index_score", sa.Integer(), nullable=True),
        sa.Column("manual_locked", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("resource_state", sa.String(length=16), nullable=True),
        sa.Column("discovered_from", sa.String(length=255), nullable=True),
        sa.Column("discovered_by", sa.String(length=16), nullable=True),
        sa.Column("source_url", sa.String(length=255), nullable=True),
        sa.Column("is_blacklisted", sa.Boolean(), nullable=False),
        sa.Column("blacklist_reason", sa.String(length=255), nullable=True),
        sa.Column("is_favorite", sa.Boolean(), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("adopted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("adopted_by", sa.String(length=64), nullable=True),
        sa.Column("adopted_account_id", sa.Integer(), nullable=True),
        sa.Column("adopted_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("adopted_lead_count", sa.Integer(), nullable=True),
        sa.Column("last_probed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("probe_error", sa.String(length=255), nullable=True),
        sa.Column("next_refresh_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["adopted_account_id"],
            ["tg_accounts.id"],
            name=op.f("fk_tg_resources_adopted_account_id_tg_accounts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tg_resources")),
    )
    with op.batch_alter_table("tg_resources", schema=None) as batch_op:
        batch_op.create_index(op.f("ix_tg_resources_tg_id"), ["tg_id"], unique=True)
        batch_op.create_index(op.f("ix_tg_resources_invite_link"), ["invite_link"], unique=False)
        batch_op.create_index(op.f("ix_tg_resources_status"), ["status"], unique=False)
        batch_op.create_index(
            op.f("ix_tg_resources_is_index_group"), ["is_index_group"], unique=False
        )
        batch_op.create_index(op.f("ix_tg_resources_language"), ["language"], unique=False)
        batch_op.create_index(
            op.f("ix_tg_resources_activity_score"), ["activity_score"], unique=False
        )
        batch_op.create_index(op.f("ix_tg_resources_member_count"), ["member_count"], unique=False)
        batch_op.create_index(
            op.f("ix_tg_resources_next_refresh_at"), ["next_refresh_at"], unique=False
        )
        batch_op.create_index(op.f("ix_tg_resources_chat_type"), ["chat_type"], unique=False)
        batch_op.create_index(
            op.f("ix_tg_resources_is_blacklisted"), ["is_blacklisted"], unique=False
        )
        batch_op.create_index(op.f("ix_tg_resources_is_favorite"), ["is_favorite"], unique=False)

    op.create_table(
        "resource_probe_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("probed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=True),
        sa.Column("activity_score", sa.Float(), nullable=True),
        sa.Column("posts_per_day", sa.Float(), nullable=True),
        sa.Column("human_ratio", sa.Float(), nullable=True),
        sa.Column("link_density", sa.Float(), nullable=True),
        sa.Column("lead_potential", sa.Float(), nullable=True),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=255), nullable=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("requests_used", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["resource_id"],
            ["tg_resources.id"],
            name=op.f("fk_resource_probe_logs_resource_id_tg_resources"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["tg_accounts.id"],
            name=op.f("fk_resource_probe_logs_account_id_tg_accounts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resource_probe_logs")),
    )
    with op.batch_alter_table("resource_probe_logs", schema=None) as batch_op:
        batch_op.create_index(
            op.f("ix_resource_probe_logs_resource_id"), ["resource_id"], unique=False
        )
        batch_op.create_index(op.f("ix_resource_probe_logs_probed_at"), ["probed_at"], unique=False)
        batch_op.create_index(op.f("ix_resource_probe_logs_result"), ["result"], unique=False)
        batch_op.create_index(
            "ix_resource_probe_resource_time",
            ["resource_id", "probed_at"],
            unique=False,
        )

    op.create_table(
        "resource_discover_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("keyword", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("new_found", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column("flood_waits", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resource_discover_tasks")),
        sa.UniqueConstraint("kind", "keyword", name="uq_discover_kind_keyword"),
    )
    with op.batch_alter_table("resource_discover_tasks", schema=None) as batch_op:
        batch_op.create_index(op.f("ix_resource_discover_tasks_kind"), ["kind"], unique=False)
        batch_op.create_index(op.f("ix_resource_discover_tasks_enabled"), ["enabled"], unique=False)
        batch_op.create_index(
            op.f("ix_resource_discover_tasks_next_run_at"), ["next_run_at"], unique=False
        )

    op.create_table(
        "resource_join_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["resource_id"],
            ["tg_resources.id"],
            name=op.f("fk_resource_join_tasks_resource_id_tg_resources"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["tg_accounts.id"],
            name=op.f("fk_resource_join_tasks_account_id_tg_accounts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resource_join_tasks")),
    )
    with op.batch_alter_table("resource_join_tasks", schema=None) as batch_op:
        batch_op.create_index(
            op.f("ix_resource_join_tasks_resource_id"), ["resource_id"], unique=False
        )
        batch_op.create_index(op.f("ix_resource_join_tasks_status"), ["status"], unique=False)
        batch_op.create_index(
            op.f("ix_resource_join_tasks_scheduled_at"), ["scheduled_at"], unique=False
        )

    op.create_table(
        "resource_quotas",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("searches", sa.Integer(), nullable=False),
        sa.Column("joins", sa.Integer(), nullable=False),
        sa.Column("leaves", sa.Integer(), nullable=False),
        sa.Column("probes", sa.Integer(), nullable=False),
        sa.Column("flood_waits", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["tg_accounts.id"],
            name=op.f("fk_resource_quotas_account_id_tg_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resource_quotas")),
        sa.UniqueConstraint("account_id", "day", name="uq_quota_account_day"),
    )
    with op.batch_alter_table("resource_quotas", schema=None) as batch_op:
        batch_op.create_index(op.f("ix_resource_quotas_account_id"), ["account_id"], unique=False)
        batch_op.create_index(op.f("ix_resource_quotas_day"), ["day"], unique=False)


def downgrade() -> None:
    op.drop_table("resource_quotas")
    op.drop_table("resource_join_tasks")
    op.drop_table("resource_discover_tasks")
    op.drop_table("resource_probe_logs")
    op.drop_table("tg_resources")
