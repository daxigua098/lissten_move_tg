"""多租户地基（P1-03）：chats 拆成群目录 + 租户群关系。

覆盖三件事：

1. 迁移把存量 chats 拆到两张表，**ID 沿用**，线路 / 目标 / 水位线 / 线索的引用值不变；
2. 同一个群可以被两个租户各自配置，备注与角色互不影响，目录只有一份；
3. 删掉某个租户的配置不会动到目录，也不会动到别的租户。
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import func, select

from app.core.telegram_client import ChatProfile
from app.db.models import (
    SELF_TENANT_ID,
    TENANT_KIND_MEMBER,
    ChatDirectory,
    TenantChat,
)
from app.services import chat_service, tenant_service, user_service

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def database(api_config) -> AsyncIterator[object]:
    from app.db.session import create_schema, dispose_database, init_database

    await init_database(api_config)
    await create_schema()
    try:
        yield api_config
    finally:
        await dispose_database()


def _alembic_config(database_path: Path) -> AlembicConfig:
    config = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    return config


def _profile(tg_id: int = 9001, title: str = "测试群") -> ChatProfile:
    return ChatProfile(
        tg_id=tg_id,
        chat_type="supergroup",
        title=title,
        username="testgroup",
        is_private=False,
        member_count=120,
    )


async def _create_member_tenant(config, *, name: str, username: str) -> int:
    from app.db.session import session_scope

    async with session_scope() as session:
        user = await user_service.create_user(
            session,
            config,
            username=username,
            password="Member12345",
            role="viewer",
        )
        owner_id = user.id

    async with session_scope() as session:
        tenant = await tenant_service.create_tenant(
            session,
            name=name,
            kind=TENANT_KIND_MEMBER,
            owner_user_id=owner_id,
        )
        return tenant.id


def test_models_split_objective_and_tenant_fields() -> None:
    """模型口径：客观字段在目录表，主观字段在租户表。"""
    directory_columns = set(ChatDirectory.__table__.columns.keys())
    tenant_columns = set(TenantChat.__table__.columns.keys())

    assert {"tg_id", "chat_type", "title", "username", "is_private", "member_count"} <= (
        directory_columns
    )
    assert {"display_name", "tags", "is_source", "is_target", "target_role", "note"} <= (
        tenant_columns
    )
    # 客观字段不再落在租户表上
    assert "tg_id" not in tenant_columns
    assert "title" not in tenant_columns


def test_migration_splits_chats_and_keeps_references(tmp_path: Path) -> None:
    """存量 chats 拆到两张表：ID 沿用，外键改指，水位线与线索引用值不变。"""
    database_path = tmp_path / "chats_split.db"
    config = _alembic_config(database_path)
    command.upgrade(config, "0018_tenant_ownership")

    connection = sqlite3.connect(str(database_path))
    try:
        with connection:
            connection.execute(
                "INSERT INTO chats (id, tg_id, chat_type, title, username, is_private, "
                "joined, can_post, member_count, tags, source_kind, note, created_at, "
                "updated_at, is_source, source_enabled, is_target, target_enabled, "
                "target_role, display_name) "
                "VALUES (5, 9001, 'supergroup', '旧群', 'jq', 0, 1, NULL, 42, '[\"a\"]', "
                "'local', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1, 1, 1, 1, 'lead', "
                "'备注名')"
            )
            connection.execute(
                "INSERT INTO routes (tenant_id, name, source_chat_id, business_type, "
                "sender_mode, priority, delay_seconds, enabled, a_config, b_config, "
                "created_at, updated_at) "
                "VALUES (1, '线路', 5, 'A', 'account', 100, 1.0, 1, '{}', '{}', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "INSERT INTO route_targets (tenant_id, route_id, target_chat_id, "
                "target_role, enabled, created_at, updated_at) "
                "VALUES (1, 1, 5, 'lead', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "INSERT INTO route_target_progress (tenant_id, route_id, target_chat_id, "
                "last_delivered_message_id, backfill_status, created_at, updated_at) "
                "VALUES (1, 1, 5, 777, 'idle', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "INSERT INTO leads (tenant_id, source_chat_id, message_id, contacts, "
                "matched_mode, score, text, source_title, delivered, created_at, updated_at) "
                "VALUES (1, 5, 99, '', '', 0.0, '旧线索', '旧群', 0, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP)"
            )
    finally:
        connection.close()

    command.upgrade(config, "head")

    connection = sqlite3.connect(str(database_path))
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "chats" not in tables
        assert {"chat_directory", "tenant_chats"} <= tables

        directory = connection.execute(
            "SELECT id, tg_id, chat_type, title, username, member_count FROM chat_directory"
        ).fetchall()
        assert directory == [(5, 9001, "supergroup", "旧群", "jq", 42)]

        tenant_rows = connection.execute(
            "SELECT id, tenant_id, chat_id, display_name, tags, is_source, is_target, "
            "target_role FROM tenant_chats"
        ).fetchall()
        assert tenant_rows == [(5, 1, 5, "备注名", '["a"]', 1, 1, "lead")]

        # 引用值一个都没变，只是外键改指了 tenant_chats
        assert connection.execute("SELECT source_chat_id FROM routes").fetchall() == [(5,)]
        assert connection.execute("SELECT target_chat_id FROM route_targets").fetchall() == [(5,)]
        assert connection.execute(
            "SELECT last_delivered_message_id FROM route_target_progress"
        ).fetchall() == [(777,)]
        assert connection.execute("SELECT source_chat_id FROM leads").fetchall() == [(5,)]

        fk_targets = {row[2] for row in connection.execute("PRAGMA foreign_key_list(routes)")}
        assert fk_targets == {"tenant_chats", "tg_accounts", "control_bots", "tenants"}
        assert {row[2] for row in connection.execute("PRAGMA foreign_key_list(leads)")} == {
            "tenant_chats",
            "routes",
            "tenants",
        }

        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_migration_downgrade_restores_single_chats_table(tmp_path: Path) -> None:
    """降级把自营租户的群配置还原回单表 chats（回滚可用）。"""
    database_path = tmp_path / "chats_roundtrip.db"
    config = _alembic_config(database_path)
    command.upgrade(config, "head")
    command.downgrade(config, "0018_tenant_ownership")

    connection = sqlite3.connect(str(database_path))
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "chats" in tables
        assert "tenant_chats" not in tables
        assert "chat_directory" not in tables
        fk_targets = {row[2] for row in connection.execute("PRAGMA foreign_key_list(routes)")}
        assert "chats" in fk_targets
    finally:
        connection.close()


async def test_two_tenants_share_one_directory(database) -> None:
    """同一个群被两个租户配置：目录一份，租户配置各一份且互不影响。"""
    from app.db.session import session_scope

    other_tenant_id = await _create_member_tenant(database, name="客户乙", username="member-f")

    async with session_scope() as session:
        first = await chat_service.upsert_chat_from_profile(session, _profile(), joined=True)

    async with session_scope() as session:
        second = await chat_service.upsert_chat_from_profile(
            session,
            _profile(title="改了标题也不影响别人"),
            joined=True,
            tenant_id=other_tenant_id,
        )
        second.display_name = "客户乙的备注"
        await session.commit()

    assert first.id != second.id
    assert first.directory.id == second.directory.id

    async with session_scope() as session:
        directories = await session.scalar(select(func.count()).select_from(ChatDirectory))
        tenant_rows = await session.scalar(select(func.count()).select_from(TenantChat))

    assert directories == 1
    assert tenant_rows == 2

    async with session_scope() as session:
        mine = await chat_service.get_chat(session, first.id)
        theirs = await chat_service.get_chat(session, second.id)

    assert mine is not None and theirs is not None
    assert mine.display_name is None
    assert theirs.display_name == "客户乙的备注"
    # 客观信息共享同一份目录
    assert mine.tg_id == theirs.tg_id == 9001


async def test_tenant_lists_are_isolated(database) -> None:
    """列表按租户隔离：客户乙看不到自营租户配的监听源。"""
    from app.db.session import session_scope

    other_tenant_id = await _create_member_tenant(database, name="客户丙", username="member-g")

    async with session_scope() as session:
        mine = await chat_service.upsert_chat_from_profile(session, _profile(), joined=True)
        await chat_service.set_source(session, mine, enabled=True)

    async with session_scope() as session:
        own_rows, own_total = await chat_service.list_sources(session)
        other_rows, other_total = await chat_service.list_sources(
            session,
            tenant_id=other_tenant_id,
        )

    assert own_total == 1
    assert other_total == 0
    assert own_rows[0].tenant_id == SELF_TENANT_ID
    assert other_rows == []


async def test_deleting_tenant_chat_keeps_shared_directory(database) -> None:
    """删掉本租户的群配置：目录保留，别的租户不受影响。"""
    from app.db.session import session_scope

    other_tenant_id = await _create_member_tenant(database, name="客户丁", username="member-h")

    async with session_scope() as session:
        mine = await chat_service.upsert_chat_from_profile(session, _profile(), joined=True)

    async with session_scope() as session:
        await chat_service.upsert_chat_from_profile(
            session,
            _profile(),
            joined=True,
            tenant_id=other_tenant_id,
        )

    async with session_scope() as session:
        await chat_service.delete_chat(session, mine.id)

    async with session_scope() as session:
        directories = await session.scalar(select(func.count()).select_from(ChatDirectory))
        tenant_rows = await session.scalar(select(func.count()).select_from(TenantChat))
        theirs = await chat_service.list_pool(session, tenant_id=other_tenant_id)

    assert directories == 1
    assert tenant_rows == 1
    assert theirs[1] == 1


async def test_migrate_chat_moves_directory_and_keeps_tenant_id(database) -> None:
    """群升级成超级群：目录换 tg_id，租户侧 ID 不变（线路引用不丢）。"""
    from app.db.session import session_scope

    async with session_scope() as session:
        chat = await chat_service.upsert_chat_from_profile(session, _profile(9001), joined=True)

    async with session_scope() as session:
        moved = await chat_service.migrate_chat(
            session,
            old_tg_id=9001,
            profile=_profile(9002, title="升级后的群"),
        )

    assert moved is not None
    assert moved.id == chat.id
    assert moved.tg_id == 9002
    assert moved.title == "升级后的群"
