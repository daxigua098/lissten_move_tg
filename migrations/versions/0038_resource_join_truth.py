"""修正历史监听源里没有成功加群证据的假“已加入”。

Revision ID: 0038_resource_join_truth
Revises: 0037_outreach_records
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0038_resource_join_truth"
down_revision: str | None = "0037_outreach_records"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """把“资源已采纳但从未成功加群”的历史监听源降级为未加入。"""
    bind = op.get_bind()
    tenant_chats = sa.table(
        "tenant_chats",
        sa.column("chat_id", sa.Integer),
        sa.column("tenant_id", sa.Integer),
        sa.column("joined", sa.Boolean),
        sa.column("source_enabled", sa.Boolean),
        sa.column("is_source", sa.Boolean),
    )
    chat_directory = sa.table(
        "chat_directory",
        sa.column("id", sa.Integer),
        sa.column("tg_id", sa.BigInteger),
    )
    tg_resources = sa.table(
        "tg_resources",
        sa.column("id", sa.Integer),
        sa.column("tenant_id", sa.Integer),
        sa.column("tg_id", sa.BigInteger),
        sa.column("status", sa.String),
    )
    resource_join_tasks = sa.table(
        "resource_join_tasks",
        sa.column("resource_id", sa.Integer),
        sa.column("tenant_id", sa.Integer),
        sa.column("action", sa.String),
        sa.column("status", sa.String),
    )

    adopted_resource = sa.exists(
        sa.select(1)
        .select_from(
            tg_resources.join(
                chat_directory,
                chat_directory.c.tg_id == tg_resources.c.tg_id,
            )
        )
        .where(
            chat_directory.c.id == tenant_chats.c.chat_id,
            tg_resources.c.tenant_id == tenant_chats.c.tenant_id,
            tg_resources.c.status == "adopted",
        )
    )
    successful_join = sa.exists(
        sa.select(1)
        .select_from(
            resource_join_tasks.join(
                tg_resources,
                tg_resources.c.id == resource_join_tasks.c.resource_id,
            ).join(
                chat_directory,
                chat_directory.c.tg_id == tg_resources.c.tg_id,
            )
        )
        .where(
            chat_directory.c.id == tenant_chats.c.chat_id,
            tg_resources.c.tenant_id == tenant_chats.c.tenant_id,
            resource_join_tasks.c.tenant_id == tenant_chats.c.tenant_id,
            resource_join_tasks.c.action == "join",
            resource_join_tasks.c.status == "success",
        )
    )

    bind.execute(
        tenant_chats.update()
        .where(
            tenant_chats.c.is_source.is_(True),
            tenant_chats.c.joined.is_(True),
            adopted_resource,
            ~successful_join,
        )
        .values(joined=False, source_enabled=False)
    )


def downgrade() -> None:
    """只修正错误状态，不回滚为可能错误的“已加入”。"""
    pass
