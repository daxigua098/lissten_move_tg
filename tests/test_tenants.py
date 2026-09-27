"""多租户地基（P1-01 / P1-02）：tenants 表、账号归属字段与唯一约束。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, UserExistsError, ValidationFailedError
from app.db.models import (
    ACCOUNT_TYPE_MEMBER,
    ACCOUNT_TYPE_PLATFORM,
    ACCOUNT_TYPES,
    ROLE_SUPER_ADMIN,
    SELF_TENANT_ID,
    SELF_TENANT_NAME,
    TENANT_KIND_MEMBER,
    TENANT_KIND_SELF,
    TENANT_STATUS_ACTIVE,
    TENANT_STATUS_SUSPENDED,
    TENANT_STATUSES,
    Tenant,
    User,
)
from app.services import tenant_service, user_service

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def database(api_config) -> AsyncIterator[object]:
    """已建表的临时库（直接用 ORM 元数据建表，覆盖 service 层）。"""
    from app.db.session import create_schema, dispose_database, init_database

    await init_database(api_config)
    await create_schema()
    try:
        yield api_config
    finally:
        await dispose_database()


def _migrate_to_head(database_path: Path) -> None:
    """在临时 SQLite 文件上执行全部迁移。"""
    config = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")


def test_migration_seeds_self_tenant_with_fixed_id(tmp_path: Path) -> None:
    """迁移后自营租户必须存在且固定为 id=1（存量数据的归属）。"""
    database_path = tmp_path / "tenants.db"
    _migrate_to_head(database_path)

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with Session(engine) as session:
            rows = session.execute(
                text("SELECT id, name, kind, status, expires_at FROM tenants")
            ).all()
    finally:
        engine.dispose()

    assert len(rows) == 1
    tenant_id, name, kind, status, expires_at = rows[0]
    assert tenant_id == SELF_TENANT_ID
    assert name == SELF_TENANT_NAME
    assert kind == TENANT_KIND_SELF
    assert status == TENANT_STATUS_ACTIVE
    assert expires_at is None


def test_migration_backfills_existing_accounts_as_platform(tmp_path: Path) -> None:
    """存量账号回填 account_type='platform'、tenant_id 为空。"""
    database_path = tmp_path / "backfill.db"
    _migrate_to_head(database_path)

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (username, password_hash, role, enabled, "
                    "must_change_password, is_builtin, created_at, updated_at) "
                    "VALUES ('legacy', 'hash', 'super_admin', 1, 0, 0, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
        with Session(engine) as session:
            row = session.execute(
                text("SELECT account_type, tenant_id FROM users WHERE username = 'legacy'")
            ).one()
    finally:
        engine.dispose()

    assert row[0] == ACCOUNT_TYPE_PLATFORM
    assert row[1] is None


async def test_ensure_self_tenant_is_idempotent(database) -> None:
    """自营租户保底：重复调用只留一行，且主键固定为 1。"""
    from app.db.session import session_scope

    async with session_scope() as session:
        first = await tenant_service.ensure_self_tenant(session)
    async with session_scope() as session:
        second = await tenant_service.ensure_self_tenant(session)
        total = await tenant_service.count_tenants(session)

    assert first.id == SELF_TENANT_ID
    assert second.id == SELF_TENANT_ID
    assert total == 1


async def test_create_member_tenant_binds_owner(database) -> None:
    """会员租户必须绑定登录账号，且账号与租户 1:1。"""
    from app.db.session import session_scope

    async with session_scope() as session:
        await tenant_service.ensure_self_tenant(session)
        owner = await user_service.create_user(
            session,
            database,
            username="member-a",
            password="Member12345",
            role="viewer",
        )
        owner_id = owner.id

    async with session_scope() as session:
        tenant = await tenant_service.create_tenant(
            session,
            name="客户 A",
            kind=TENANT_KIND_MEMBER,
            owner_user_id=owner_id,
            created_by="agent-a",
        )
        assert tenant.kind == TENANT_KIND_MEMBER
        assert tenant.status == TENANT_STATUS_ACTIVE
        assert tenant.owner_user_id == owner_id

    async with session_scope() as session:
        found = await tenant_service.get_tenant_by_owner(session, owner_id)
        assert found is not None

        # 同一账号不能再开第二个租户
        with pytest.raises(ConflictError):
            await tenant_service.create_tenant(
                session,
                name="客户 A2",
                kind=TENANT_KIND_MEMBER,
                owner_user_id=owner_id,
            )


async def test_member_tenant_without_owner_is_rejected(database) -> None:
    """会员租户缺登录账号：service 报错，数据库 CHECK 也兜底。"""
    from app.db.session import session_scope

    async with session_scope() as session:
        await tenant_service.ensure_self_tenant(session)

    async with session_scope() as session:
        with pytest.raises(ValidationFailedError):
            await tenant_service.create_tenant(session, name="无主租户")

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(Tenant(name="无主租户", kind=TENANT_KIND_MEMBER))
            await session.flush()


async def test_tenant_name_is_globally_unique(database) -> None:
    """租户名全局唯一：两个客户不能重名。"""
    from app.db.session import session_scope

    async with session_scope() as session:
        await tenant_service.create_tenant(session, name="客户甲", kind=TENANT_KIND_SELF)

    async with session_scope() as session:
        with pytest.raises(ConflictError):
            await tenant_service.create_tenant(session, name="客户甲", kind=TENANT_KIND_SELF)

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(Tenant(name="客户甲", kind=TENANT_KIND_SELF))
            await session.flush()


async def test_new_user_defaults_to_platform_without_tenant(database) -> None:
    """现有建号路径默认落在平台类型、无租户（不改变现有行为）。"""
    from app.db.session import session_scope

    async with session_scope() as session:
        user = await user_service.create_user(
            session,
            database,
            username="platform-a",
            password="Platform12345",
            role=ROLE_SUPER_ADMIN,
        )

    assert user.account_type == ACCOUNT_TYPE_PLATFORM
    assert user.tenant_id is None


async def test_username_stays_globally_unique_across_tenants(database) -> None:
    """用户名必须全局唯一：租户不同也不能重名（登录需全局寻址）。"""
    from app.db.session import session_scope

    async with session_scope() as session:
        first = await user_service.create_user(
            session,
            database,
            username="same-name",
            password="SameName12345",
            role="viewer",
        )
        assert first.account_type == ACCOUNT_TYPE_PLATFORM

    async with session_scope() as session:
        with pytest.raises(UserExistsError):
            await user_service.create_user(
                session,
                database,
                username="same-name",
                password="SameName12345",
                role="viewer",
            )


async def test_tenant_listing_filters_by_kind_and_status(database) -> None:
    """租户列表按类型、状态过滤，供平台后台使用。"""
    from app.db.session import session_scope

    async with session_scope() as session:
        await tenant_service.ensure_self_tenant(session)
        owner = await user_service.create_user(
            session,
            database,
            username="member-b",
            password="Member12345",
            role="viewer",
        )
        owner_id = owner.id

    async with session_scope() as session:
        await tenant_service.create_tenant(
            session,
            name="客户乙",
            kind=TENANT_KIND_MEMBER,
            owner_user_id=owner_id,
            status=TENANT_STATUS_SUSPENDED,
        )

    async with session_scope() as session:
        rows, total = await tenant_service.list_tenants(session)
        assert total == 2
        assert [row.kind for row in rows] == [TENANT_KIND_SELF, TENANT_KIND_MEMBER]

        suspended, suspended_total = await tenant_service.list_tenants(
            session,
            status=TENANT_STATUS_SUSPENDED,
        )
        assert suspended_total == 1
        assert suspended[0].name == "客户乙"


def test_account_and_tenant_constants_cover_design_values() -> None:
    """账号类型与租户状态常量覆盖面（防止后续改名漏改）。"""
    assert ACCOUNT_TYPE_MEMBER in ACCOUNT_TYPES
    assert ACCOUNT_TYPE_PLATFORM in ACCOUNT_TYPES
    assert TENANT_STATUS_ACTIVE in TENANT_STATUSES


async def test_users_can_be_queried_by_tenant_column(database) -> None:
    """users.tenant_id 可查询（后续查询收口的基础）。"""
    from app.db.session import session_scope

    async with session_scope() as session:
        session.add(User(username="tenant-bound", password_hash="h", tenant_id=None))
        await session.commit()

    async with session_scope() as session:
        rows = list(await session.scalars(select(User).where(User.tenant_id.is_(None))))

    assert [row.username for row in rows] == ["tenant-bound"]
