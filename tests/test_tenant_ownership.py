"""多租户地基（P1-03/P1-04 前置）：业务表归属字段、唯一约束重审与回填。"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    SELF_TENANT_ID,
    TENANT_KIND_MEMBER,
    AdAsset,
    HotKeyword,
    Lead,
    MemberProfile,
    Tenant,
)
from app.services import tenant_service, tg_account_service, user_service

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 必须带 tenant_id 的业务表（对应字段级设计第 3 节的归属总表）
TENANT_OWNED_TABLES = (
    "tg_accounts",
    "control_bots",
    "routes",
    "route_targets",
    "route_target_progress",
    "delivery_jobs",
    "ad_assets",
    "keyword_groups",
    "keywords",
    "leads",
    "member_profiles",
    "hot_keywords",
    "tg_resources",
    "resource_probe_logs",
    "resource_discover_tasks",
    "resource_directory_runs",
    "resource_join_tasks",
    "resource_quotas",
)

OPTIONAL_TENANT_TABLES = ("web_sessions", "login_history", "audit_logs")


@pytest.fixture
async def database(api_config) -> AsyncIterator[object]:
    """已建表（含自营租户）的临时库。"""
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


async def _create_member_tenant(config, *, name: str, username: str) -> int:
    """开一个会员租户（带登录账号），返回租户 ID。"""
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


def test_models_declare_tenant_ownership() -> None:
    """模型层：该带的表都带 tenant_id，且非空。"""
    from app.db.base import Base

    for table_name in TENANT_OWNED_TABLES:
        column = Base.metadata.tables[table_name].columns["tenant_id"]
        assert column.nullable is False, f"{table_name}.tenant_id 必须非空"
    for table_name in OPTIONAL_TENANT_TABLES:
        column = Base.metadata.tables[table_name].columns["tenant_id"]
        assert column.nullable is True, f"{table_name}.tenant_id 只记录归属，可为空"


def test_migration_adds_tenant_columns_and_rescopes_unique(tmp_path: Path) -> None:
    """迁移后：业务表带 tenant_id，单列唯一索引换成租户内复合唯一。"""
    database_path = tmp_path / "ownership.db"
    command.upgrade(_alembic_config(database_path), "head")

    connection = sqlite3.connect(str(database_path))
    try:
        for table_name in TENANT_OWNED_TABLES:
            rows = connection.execute(f"PRAGMA table_info({table_name})")
            columns = {row[1]: row for row in rows}
            assert "tenant_id" in columns, f"{table_name} 缺 tenant_id"
            assert columns["tenant_id"][3] == 1, f"{table_name}.tenant_id 必须 NOT NULL"

        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'tg_accounts'"
        ).fetchone()[0]
        assert "CONSTRAINT uq_tg_accounts_tenant_name UNIQUE (tenant_id, name)" in ddl

        discover_ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'resource_discover_tasks'"
        ).fetchone()[0]
        assert "uq_discover_tenant_kind_keyword" in discover_ddl
    finally:
        connection.close()


def test_migration_backfills_legacy_rows_to_self_tenant(tmp_path: Path) -> None:
    """存量数据一次性回填自营租户，行数不变。"""
    database_path = tmp_path / "backfill.db"
    config = _alembic_config(database_path)
    command.upgrade(config, "0017_tenants")

    connection = sqlite3.connect(str(database_path))
    try:
        with connection:
            connection.execute(
                "INSERT INTO ad_assets (name, text, enabled, created_at, updated_at) "
                "VALUES ('旧文案', '', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "INSERT INTO hot_keywords (token, count, message_count, sources, "
                "created_at, updated_at) "
                "VALUES ('旧热词', 3, 3, '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "INSERT INTO member_profiles (tg_user_id, is_bot, message_count, pinned, "
                "created_at, updated_at) "
                "VALUES (9001, 0, 2, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "INSERT INTO tg_accounts (name, phone_masked, phone_enc, api_id_enc, "
                "api_hash_enc, session_name, is_default, status, health_score, "
                "consecutive_failures, created_at, updated_at) "
                "VALUES ('旧主号', '138****0000', 'enc', 'enc', 'enc', 'sess', 1, "
                "'active', 100, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "INSERT INTO leads (message_id, contacts, matched_mode, score, text, "
                "source_title, delivered, created_at, updated_at) "
                "VALUES (12345, '[]', 'contains', 1.0, '旧线索', '旧群', 0, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
    finally:
        connection.close()

    command.upgrade(config, "head")

    connection = sqlite3.connect(str(database_path))
    try:
        for table_name in ("ad_assets", "hot_keywords", "member_profiles", "tg_accounts", "leads"):
            total = connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            owned = connection.execute(
                f"SELECT COUNT(*) FROM {table_name} WHERE tenant_id = {SELF_TENANT_ID}"
            ).fetchone()[0]
            assert total == owned == 1, f"{table_name} 回填异常：total={total} owned={owned}"

        for table_name in TENANT_OWNED_TABLES:
            assert connection.execute(f"PRAGMA foreign_key_check({table_name})").fetchall() == []
    finally:
        connection.close()


def test_migration_roundtrip_restores_single_column_unique(tmp_path: Path) -> None:
    """降级回 0017 后，单列唯一索引必须还原（回滚可用）。"""
    database_path = tmp_path / "roundtrip.db"
    config = _alembic_config(database_path)
    command.upgrade(config, "head")
    command.downgrade(config, "0017_tenants")

    connection = sqlite3.connect(str(database_path))
    try:
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'ix_tg_accounts_name'"
        ).fetchone()[0]
        assert "UNIQUE" in ddl.upper()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tg_accounts)")}
        assert "tenant_id" not in columns
    finally:
        connection.close()


async def test_create_schema_seeds_self_tenant(database) -> None:
    """本地/测试建表路径必须自带自营租户，否则业务写入会被外键挡下。"""
    from app.db.session import session_scope

    async with session_scope() as session:
        tenant = await tenant_service.get_tenant(session, SELF_TENANT_ID)

    assert tenant is not None
    assert tenant.name == "自营"


async def test_new_business_rows_default_to_self_tenant(database) -> None:
    """尚未接身份的写入路径落在自营租户（单租户兼容口径）。"""
    from app.db.session import session_scope

    async with session_scope() as session:
        await tg_account_service.create_account(
            session,
            database,
            name="主号",
            phone="+8613800001111",
            api_id=123456,
            api_hash="abcdef0123456789abcdef0123456789",
            is_default=True,
        )
        session.add(AdAsset(name="默认文案"))
        session.add(Lead(message_id=1))
        await session.commit()

    async with session_scope() as session:
        account = await tg_account_service.get_default_account(session)
        assert account is not None
        assert account.tenant_id == SELF_TENANT_ID

        asset = await session.scalar(text("SELECT tenant_id FROM ad_assets"))
        lead_tenant = await session.scalar(text("SELECT tenant_id FROM leads"))
        assert asset == SELF_TENANT_ID
        assert lead_tenant == SELF_TENANT_ID


async def test_tenant_id_cannot_be_null(database) -> None:
    """tenant_id 非空：绕过 ORM 直接写 NULL 必须被数据库拒绝。"""
    from app.db.session import session_scope

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            await session.execute(
                text(
                    "INSERT INTO ad_assets "
                    "(name, text, enabled, tenant_id, created_at, updated_at) "
                    "VALUES ('无主文案', '', 1, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            await session.flush()


async def test_account_name_unique_within_tenant_only(database) -> None:
    """账号名只在租户内唯一：两个客户都可以有"主号"。"""
    from app.db.session import session_scope

    other_tenant_id = await _create_member_tenant(database, name="客户乙", username="member-c")

    async with session_scope() as session:
        await tg_account_service.create_account(
            session,
            database,
            name="主号",
            phone="+8613800001111",
            api_id=123456,
            api_hash="abcdef0123456789abcdef0123456789",
            is_default=True,
        )

    # 同一个租户里重名：被唯一约束挡下
    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(_account_row(name="主号", tenant_id=SELF_TENANT_ID))
            await session.flush()

    # 另一个租户用同一个名字：允许
    async with session_scope() as session:
        session.add(_account_row(name="主号", tenant_id=other_tenant_id))
        await session.flush()


async def test_member_profile_and_hot_keyword_are_scoped(database) -> None:
    """档案与热门词按租户分开：同一个人 / 同一个词在两个客户下各存一份。"""
    from app.db.session import session_scope

    other_tenant_id = await _create_member_tenant(database, name="客户丙", username="member-d")
    assert other_tenant_id != SELF_TENANT_ID

    async with session_scope() as session:
        session.add(MemberProfile(tg_user_id=777))
        session.add(HotKeyword(token="球赛"))
        await session.commit()

    async with session_scope() as session:
        session.add(MemberProfile(tg_user_id=777, tenant_id=other_tenant_id))
        session.add(HotKeyword(token="球赛", tenant_id=other_tenant_id))
        await session.commit()

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(MemberProfile(tg_user_id=777, tenant_id=other_tenant_id))
            await session.flush()

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(HotKeyword(token="球赛", tenant_id=other_tenant_id))
            await session.flush()


async def test_deleting_tenant_cascades_business_rows(database) -> None:
    """删租户连带删业务数据（外键 CASCADE），不会留下孤儿行。"""
    from app.db.session import session_scope

    other_tenant_id = await _create_member_tenant(database, name="客户丁", username="member-e")

    async with session_scope() as session:
        session.add(HotKeyword(token="临时词", tenant_id=other_tenant_id))
        await session.commit()

    async with session_scope() as session:
        tenant = await session.get(Tenant, other_tenant_id)
        assert tenant is not None
        await session.delete(tenant)
        await session.commit()

    async with session_scope() as session:
        remaining = await session.scalar(
            text("SELECT COUNT(*) FROM hot_keywords WHERE tenant_id = :tid"),
            {"tid": other_tenant_id},
        )

    assert remaining == 0


def _account_row(*, name: str, tenant_id: int):
    """构造一条指定租户的执行账号行（唯一约束用例用）。"""
    from app.db.models import TgAccount

    return TgAccount(
        name=name,
        tenant_id=tenant_id,
        phone_masked="138****0001",
        phone_enc="enc",
        api_id_enc="enc",
        api_hash_enc="enc",
        session_name=f"sess-{name}-{tenant_id}",
        status="active",
    )
