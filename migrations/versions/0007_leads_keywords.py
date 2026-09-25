"""B 线（会员监听）表：关键词组、关键词、会员档案与线索

Revision ID: 0007_leads_keywords
Revises: 0006_chat_display_name
Create Date: 2026-09-26

关键词的别名写在 keywords.aliases（逗号/顿号/换行分隔），
「体育 → 篮球 / 乒乓球」这类语义相近靠别名覆盖。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007_leads_keywords"
down_revision: str | None = "0006_chat_display_name"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "keyword_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_keyword_groups")),
    )
    op.create_index(op.f("ix_keyword_groups_name"), "keyword_groups", ["name"], unique=True)

    op.create_table(
        "keywords",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("word", sa.String(length=64), nullable=False),
        sa.Column("aliases", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["keyword_groups.id"],
            name=op.f("fk_keywords_group_id_keyword_groups"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_keywords")),
        sa.UniqueConstraint("group_id", "word", name="uq_keywords_group_id_word"),
    )
    op.create_index(op.f("ix_keywords_group_id"), "keywords", ["group_id"], unique=False)

    op.create_table(
        "member_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("display_name", sa.String(length=128), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_member_profiles")),
    )
    op.create_index(
        op.f("ix_member_profiles_tg_user_id"),
        "member_profiles",
        ["tg_user_id"],
        unique=True,
    )

    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("route_id", sa.Integer(), nullable=False),
        sa.Column("source_chat_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sender_tg_id", sa.BigInteger(), nullable=True),
        sa.Column("sender_username", sa.String(length=64), nullable=True),
        sa.Column("sender_name", sa.String(length=128), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("wechat", sa.String(length=64), nullable=True),
        sa.Column("contacts", sa.Text(), nullable=False, server_default=""),
        sa.Column("keyword", sa.String(length=64), nullable=True),
        sa.Column("keyword_group_id", sa.Integer(), nullable=True),
        sa.Column("matched_mode", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_title", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("delivered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("target_chat_id", sa.Integer(), nullable=True),
        sa.Column("target_message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["route_id"],
            ["routes.id"],
            name=op.f("fk_leads_route_id_routes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_chat_id"],
            ["chats.id"],
            name=op.f("fk_leads_source_chat_id_chats"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leads")),
    )
    op.create_index(op.f("ix_leads_route_id"), "leads", ["route_id"], unique=False)
    op.create_index(op.f("ix_leads_source_chat_id"), "leads", ["source_chat_id"], unique=False)
    op.create_index(op.f("ix_leads_sender_tg_id"), "leads", ["sender_tg_id"], unique=False)
    op.create_index(op.f("ix_leads_keyword"), "leads", ["keyword"], unique=False)
    op.create_index(op.f("ix_leads_delivered"), "leads", ["delivered"], unique=False)


def downgrade() -> None:
    op.drop_table("leads")
    op.drop_table("member_profiles")
    op.drop_table("keywords")
    op.drop_table("keyword_groups")
