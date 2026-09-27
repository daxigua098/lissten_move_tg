"""Alembic 迁移测试：升降级往返、单一 head、与模型是否一致。"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from app.db import models as _models  # noqa: F401  导入以注册全部模型
from app.db.base import Base

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TABLES = {
    "users",
    "tenants",
    "web_sessions",
    "login_history",
    "audit_logs",
    "system_settings",
}


def _alembic_config(database_path: Path) -> AlembicConfig:
    config = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    return config


def _sync_url(database_path: Path) -> str:
    return f"sqlite:///{database_path.as_posix()}"


def _table_names(database_path: Path) -> set[str]:
    engine = create_engine(_sync_url(database_path))
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_migration_history_has_single_head() -> None:
    script = ScriptDirectory.from_config(_alembic_config(Path("unused.db")))

    assert len(script.get_heads()) == 1


def test_upgrade_downgrade_roundtrip(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    assert EXPECTED_TABLES <= _table_names(database_path)

    command.downgrade(config, "base")
    assert _table_names(database_path) == {"alembic_version"}

    command.upgrade(config, "head")
    assert EXPECTED_TABLES <= _table_names(database_path)


def test_resource_join_truth_migration_clears_unproven_sources(tmp_path: Path) -> None:
    """历史采纳但没有成功加群证据的监听源，要降级为未加入。"""
    database_path = tmp_path / "resource_join_truth.db"
    config = _alembic_config(database_path)
    command.upgrade(config, "0037_outreach_records")

    engine = create_engine(_sync_url(database_path))
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO chat_directory
                        (id, tg_id, chat_type, is_private, source_kind, created_at, updated_at)
                    VALUES
                        (10, 9001, 'group', 0, 'local', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                        (11, 9002, 'group', 0, 'local', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO tenant_chats
                        (id, tenant_id, chat_id, tags, is_source, source_enabled,
                         is_target, target_enabled, target_role, joined, created_at, updated_at)
                    VALUES
                        (10, 1, 10, '[]', 1, 1, 0, 0, 'content', 1,
                         CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                        (11, 1, 11, '[]', 1, 1, 0, 0, 'content', 1,
                         CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO tg_resources
                        (id, tenant_id, tg_id, title, title_history, chat_type,
                         member_count_approx, categories, sample_messages, is_index_group,
                         manual_locked, status, is_blacklisted, is_favorite,
                         created_at, updated_at)
                    VALUES
                        (100, 1, 9001, '假已加入', '[]', 'group', 0, '[]', '[]', 0,
                         '[]', 'adopted', 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                        (101, 1, 9002, '真已加入', '[]', 'group', 0, '[]', '[]', 0,
                         '[]', 'adopted', 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO resource_join_tasks
                        (id, tenant_id, resource_id, action, status, attempts,
                         created_at, updated_at)
                    VALUES
                        (1000, 1, 101, 'join', 'success', 1,
                         CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                )
            )

        command.upgrade(config, "head")

        with engine.connect() as connection:
            rows = {
                row.id: (bool(row.joined), bool(row.source_enabled))
                for row in connection.execute(
                    sa.text("SELECT id, joined, source_enabled FROM tenant_chats ORDER BY id")
                )
            }
    finally:
        engine.dispose()

    assert rows[10] == (False, False)
    assert rows[11] == (True, True)


def test_migration_matches_models(tmp_path: Path) -> None:
    """迁移执行后的库结构必须与 ORM 模型一致（防止改模型忘记加迁移）。"""
    database_path = tmp_path / "drift.db"
    command.upgrade(_alembic_config(database_path), "head")

    engine = create_engine(_sync_url(database_path))
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    structural = [
        entry
        for entry in diff
        if entry[0] in {"add_table", "remove_table", "add_column", "remove_column"}
    ]
    assert structural == [], f"模型与迁移存在结构差异：{structural}"
