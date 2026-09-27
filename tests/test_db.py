"""数据库引擎、会话与模型测试。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import AppConfig, load_config
from app.db.models import (
    ROLE_RANK,
    ROLE_SUB_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_VIEWER,
    SystemSetting,
    User,
)
from app.db.session import (
    create_engine,
    create_schema,
    dispose_database,
    get_engine,
    get_session_factory,
    init_database,
    session_scope,
)

EXPECTED_TABLES = {
    "users",
    "tenants",
    "web_sessions",
    "login_history",
    "audit_logs",
    "system_settings",
}
VALID_SECRET_KEY = "A" * 43 + "="


def _config(project_root, **env: str) -> AppConfig:
    base = {"SECRET_KEY": VALID_SECRET_KEY, "ADMIN_PASSWORD": "custom-pass"}
    base.update(env)
    return load_config(project_root=project_root, environ=base)


def _db_url(project_root) -> str:
    return "sqlite+aiosqlite:///" + (project_root / "data" / "app.db").as_posix()


@pytest.fixture
async def database(project_root) -> AsyncIterator[AppConfig]:
    """已建表的临时数据库，测试结束释放全局状态。"""
    config = _config(project_root, DATABASE_URL=_db_url(project_root))
    await init_database(config)
    await create_schema()
    try:
        yield config
    finally:
        await dispose_database()


def test_role_rank_ordering() -> None:
    assert ROLE_RANK[ROLE_VIEWER] < ROLE_RANK[ROLE_SUB_ADMIN] < ROLE_RANK[ROLE_SUPER_ADMIN]


async def test_sqlite_pragmas_applied(project_root) -> None:
    config = _config(project_root, DATABASE_URL=_db_url(project_root))
    engine = create_engine(config)
    try:
        async with engine.connect() as connection:
            journal_mode = (await connection.execute(text("PRAGMA journal_mode"))).scalar()
            foreign_keys = (await connection.execute(text("PRAGMA foreign_keys"))).scalar()
            busy_timeout = (await connection.execute(text("PRAGMA busy_timeout"))).scalar()
    finally:
        await engine.dispose()

    assert str(journal_mode).lower() == "wal"
    assert int(foreign_keys) == 1
    assert int(busy_timeout) == 5000


async def test_create_schema_creates_expected_tables(project_root) -> None:
    config = _config(project_root, DATABASE_URL=_db_url(project_root))
    engine = create_engine(config)
    try:
        await create_schema(engine)
        async with engine.connect() as connection:
            names = (
                await connection.execute(
                    text("select name from sqlite_master where type = 'table'")
                )
            ).scalars()
            tables = set(names)
    finally:
        await engine.dispose()

    assert EXPECTED_TABLES <= tables


async def test_user_defaults_and_roundtrip(database: AppConfig) -> None:
    async with session_scope() as session:
        session.add(
            User(
                username="admin",
                password_hash="hash-value",
                role=ROLE_SUPER_ADMIN,
                is_builtin=True,
            )
        )

    async with session_scope() as session:
        user = (await session.scalars(select(User))).one()

    assert user.username == "admin"
    assert user.role == ROLE_SUPER_ADMIN
    assert user.enabled is True
    # 默认要求首次登录改密
    assert user.must_change_password is True
    assert user.is_builtin is True
    assert user.created_at is not None
    assert user.updated_at is not None


async def test_username_must_be_unique(database: AppConfig) -> None:
    async with session_scope() as session:
        session.add(User(username="dage", password_hash="h1"))

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(User(username="dage", password_hash="h2"))
            await session.flush()


async def test_session_scope_rolls_back_on_error(database: AppConfig) -> None:
    async def failing_write() -> None:
        async with session_scope() as session:
            session.add(User(username="ghost", password_hash="h"))
            raise RuntimeError("模拟业务异常")

    with pytest.raises(RuntimeError):
        await failing_write()

    async with session_scope() as session:
        count = (await session.execute(select(func.count()).select_from(User))).scalar()

    assert count == 0


async def test_system_setting_uses_key_as_primary_key(database: AppConfig) -> None:
    async with session_scope() as session:
        session.add(SystemSetting(key="retention", value='{"leads_days": 3}'))

    async with session_scope() as session:
        setting = await session.get(SystemSetting, "retention")

    assert setting is not None
    assert "leads_days" in setting.value


async def test_dispose_database_resets_global_state(project_root) -> None:
    await dispose_database()

    with pytest.raises(RuntimeError):
        get_engine()
    with pytest.raises(RuntimeError):
        get_session_factory()

    config = _config(project_root, DATABASE_URL=_db_url(project_root))
    await init_database(config)
    try:
        assert get_engine() is not None
        assert get_session_factory() is not None
    finally:
        await dispose_database()
