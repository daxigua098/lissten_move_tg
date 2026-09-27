"""监听与线索热路径复合索引

Revision ID: 0026_hot_path_indexes
Revises: 0025_standard_plan_full
Create Date: 2026-09-28
"""

from __future__ import annotations

from alembic import op

revision: str = "0026_hot_path_indexes"
down_revision: str | None = "0025_standard_plan_full"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ix_routes_tenant_enabled_source",
        "routes",
        ["tenant_id", "enabled", "source_chat_id"],
    )
    op.create_index(
        "ix_route_targets_route_enabled",
        "route_targets",
        ["route_id", "enabled"],
    )
    op.create_index(
        "ix_leads_tenant_created_at",
        "leads",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_leads_tenant_delivered",
        "leads",
        ["tenant_id", "delivered"],
    )
    op.create_index(
        "ix_leads_tenant_sender_keyword_created",
        "leads",
        ["tenant_id", "sender_tg_id", "keyword", "created_at"],
    )
    op.create_index(
        "ix_member_profiles_tenant_last_seen",
        "member_profiles",
        ["tenant_id", "last_seen_at", "pinned"],
    )
    op.create_index(
        "ix_hot_keywords_tenant_count",
        "hot_keywords",
        ["tenant_id", "count"],
    )


def downgrade() -> None:
    op.drop_index("ix_hot_keywords_tenant_count", table_name="hot_keywords")
    op.drop_index("ix_member_profiles_tenant_last_seen", table_name="member_profiles")
    op.drop_index("ix_leads_tenant_sender_keyword_created", table_name="leads")
    op.drop_index("ix_leads_tenant_delivered", table_name="leads")
    op.drop_index("ix_leads_tenant_created_at", table_name="leads")
    op.drop_index("ix_route_targets_route_enabled", table_name="route_targets")
    op.drop_index("ix_routes_tenant_enabled_source", table_name="routes")
