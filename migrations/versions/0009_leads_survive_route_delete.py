"""线索不再随线路/群删除而消失

Revision ID: 0009_leads_survive
Revises: 0008_route_bundle
Create Date: 2026-09-26

原来 leads.route_id / source_chat_id 是 ON DELETE CASCADE：删一条线路会把
它抓到的线索全部删掉（实测丢了 73 条）。线索是业务数据，改成可空 + SET NULL，
只解除引用；来源群名、发言内容、联系方式这些快照字段本来就在线索行上。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009_leads_survive"
down_revision: str | None = "0008_route_bundle"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("leads", recreate="always") as batch_op:
        batch_op.drop_constraint("fk_leads_route_id_routes", type_="foreignkey")
        batch_op.drop_constraint("fk_leads_source_chat_id_chats", type_="foreignkey")
        batch_op.alter_column("route_id", existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column("source_chat_id", existing_type=sa.Integer(), nullable=True)
        batch_op.create_foreign_key(
            "fk_leads_route_id_routes",
            "routes",
            ["route_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_leads_source_chat_id_chats",
            "chats",
            ["source_chat_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("leads", recreate="always") as batch_op:
        batch_op.drop_constraint("fk_leads_route_id_routes", type_="foreignkey")
        batch_op.drop_constraint("fk_leads_source_chat_id_chats", type_="foreignkey")
        batch_op.alter_column("route_id", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("source_chat_id", existing_type=sa.Integer(), nullable=False)
        batch_op.create_foreign_key(
            "fk_leads_route_id_routes",
            "routes",
            ["route_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_leads_source_chat_id_chats",
            "chats",
            ["source_chat_id"],
            ["id"],
            ondelete="CASCADE",
        )
