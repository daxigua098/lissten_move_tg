"""冷触达：联系锁、话术模板、租户策略与任务队列

Revision ID: 0028_outreach_queue
Revises: 0027_outreach_accounts
Create Date: 2026-09-28

P1 地基：``outreach_contacts``（全局联系锁与会话归属）、``outreach_templates``
（平台 / 会员双层话术）、``outreach_settings``（租户策略）、``outreach_tasks``
（冷触达任务队列）。本阶段只入队，不发送。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0028_outreach_queue"
down_revision: str | None = "0027_outreach_accounts"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "outreach_contacts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("display_name", sa.String(length=128), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("member_profile_id", sa.Integer(), nullable=True),
        sa.Column("first_lead_id", sa.Integer(), nullable=True),
        sa.Column("first_contact_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_contact_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_contact_account_id", sa.Integer(), nullable=True),
        sa.Column("owner_type", sa.String(length=8), nullable=False),
        sa.Column("owner_account_id", sa.Integer(), nullable=True),
        sa.Column("owner_bot_id", sa.Integer(), nullable=True),
        sa.Column("contact_count", sa.Integer(), nullable=False),
        sa.Column("follow_up_count", sa.Integer(), nullable=False),
        sa.Column("contact_state", sa.String(length=32), nullable=False),
        sa.Column("reply_state", sa.String(length=16), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("global_lock_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("do_not_contact", sa.Boolean(), nullable=False),
        sa.Column("frozen_reason", sa.String(length=64), nullable=True),
        sa.Column("identity_confirmed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_outreach_contacts_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["member_profile_id"],
            ["member_profiles.id"],
            name=op.f("fk_outreach_contacts_member_profile_id_member_profiles"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["first_lead_id"],
            ["leads.id"],
            name=op.f("fk_outreach_contacts_first_lead_id_leads"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["first_contact_account_id"],
            ["tg_accounts.id"],
            name=op.f("fk_outreach_contacts_first_contact_account_id_tg_accounts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["owner_account_id"],
            ["tg_accounts.id"],
            name=op.f("fk_outreach_contacts_owner_account_id_tg_accounts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["owner_bot_id"],
            ["control_bots.id"],
            name=op.f("fk_outreach_contacts_owner_bot_id_control_bots"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outreach_contacts")),
        sa.UniqueConstraint("tenant_id", "tg_user_id", name="uq_outreach_contacts_tenant_user"),
    )
    op.create_index(op.f("ix_outreach_contacts_tenant_id"), "outreach_contacts", ["tenant_id"])
    op.create_index(op.f("ix_outreach_contacts_tg_user_id"), "outreach_contacts", ["tg_user_id"])
    op.create_index(
        op.f("ix_outreach_contacts_contact_state"), "outreach_contacts", ["contact_state"]
    )

    op.create_table(
        "outreach_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("variables", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_template_id", sa.Integer(), nullable=True),
        sa.Column("source_version", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_outreach_templates_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_template_id"],
            ["outreach_templates.id"],
            name=op.f("fk_outreach_templates_source_template_id_outreach_templates"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outreach_templates")),
        sa.UniqueConstraint("tenant_id", "kind", "name", name="uq_outreach_templates_name"),
    )
    op.create_index(op.f("ix_outreach_templates_scope"), "outreach_templates", ["scope"])
    op.create_index(op.f("ix_outreach_templates_tenant_id"), "outreach_templates", ["tenant_id"])
    op.create_index(op.f("ix_outreach_templates_name"), "outreach_templates", ["name"])

    op.create_table(
        "outreach_settings",
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("default_cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("cross_account_lock_days", sa.Integer(), nullable=False),
        sa.Column("strict_permanent_lock", sa.Boolean(), nullable=False),
        sa.Column("follow_up_days", sa.Integer(), nullable=False),
        sa.Column("follow_up_max", sa.Integer(), nullable=False),
        sa.Column("working_hours", sa.Text(), nullable=False),
        sa.Column("daily_pool_cap", sa.Integer(), nullable=True),
        sa.Column("kill_switch", sa.Boolean(), nullable=False),
        sa.Column("delete_session_on_account_delete", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_outreach_settings_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_outreach_settings")),
    )

    op.create_table(
        "outreach_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("lead_id", sa.Integer(), nullable=True),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("template_id", sa.Integer(), nullable=True),
        sa.Column("rendered_text", sa.Text(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("target_message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_outreach_tasks_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name=op.f("fk_outreach_tasks_lead_id_leads"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["outreach_contacts.id"],
            name=op.f("fk_outreach_tasks_contact_id_outreach_contacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["tg_accounts.id"],
            name=op.f("fk_outreach_tasks_account_id_tg_accounts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["outreach_templates.id"],
            name=op.f("fk_outreach_tasks_template_id_outreach_templates"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outreach_tasks")),
        sa.UniqueConstraint("dedupe_key", name="uq_outreach_tasks_dedupe_key"),
    )
    op.create_index(op.f("ix_outreach_tasks_tenant_id"), "outreach_tasks", ["tenant_id"])
    op.create_index(op.f("ix_outreach_tasks_contact_id"), "outreach_tasks", ["contact_id"])
    op.create_index(op.f("ix_outreach_tasks_status"), "outreach_tasks", ["status"])
    op.create_index(
        "ix_outreach_tasks_tenant_status_plan",
        "outreach_tasks",
        ["tenant_id", "status", "scheduled_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outreach_tasks_tenant_status_plan", table_name="outreach_tasks")
    op.drop_index(op.f("ix_outreach_tasks_status"), table_name="outreach_tasks")
    op.drop_index(op.f("ix_outreach_tasks_contact_id"), table_name="outreach_tasks")
    op.drop_index(op.f("ix_outreach_tasks_tenant_id"), table_name="outreach_tasks")
    op.drop_table("outreach_tasks")

    op.drop_table("outreach_settings")

    op.drop_index(op.f("ix_outreach_templates_name"), table_name="outreach_templates")
    op.drop_index(op.f("ix_outreach_templates_tenant_id"), table_name="outreach_templates")
    op.drop_index(op.f("ix_outreach_templates_scope"), table_name="outreach_templates")
    op.drop_table("outreach_templates")

    op.drop_index(op.f("ix_outreach_contacts_contact_state"), table_name="outreach_contacts")
    op.drop_index(op.f("ix_outreach_contacts_tg_user_id"), table_name="outreach_contacts")
    op.drop_index(op.f("ix_outreach_contacts_tenant_id"), table_name="outreach_contacts")
    op.drop_table("outreach_contacts")
