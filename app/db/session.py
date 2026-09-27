"""数据库引擎与会话工厂。

约定：
    - API 层用 `session_dependency` 开会话，事务由 services 层提交；
    - 脚本与 CLI 用 `session_scope()`，成功自动提交、异常自动回滚；
    - `create_schema()` 仅用于测试与本地演练，生产结构一律走 Alembic 迁移。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import Connection, event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import AppConfig, absolute_database_url, sqlite_database_path
from app.core.paths import ensure_dir
from app.db import models as _models  # noqa: F401  导入以注册全部模型
from app.db.base import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine(config: AppConfig) -> AsyncEngine:
    """按配置创建异步引擎（SQLite 自动建目录并设置 PRAGMA）。"""
    url = absolute_database_url(config)
    kwargs: dict[str, Any] = {"echo": config.database.echo, "future": True}
    if _is_sqlite(url):
        database_path = sqlite_database_path(config)
        if database_path is not None:
            ensure_dir(database_path.parent)
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_async_engine(url, **kwargs)
    if _is_sqlite(url) and config.database.wal:
        _register_sqlite_pragmas(engine)
    return engine


async def init_database(config: AppConfig) -> None:
    """初始化全局引擎与会话工厂（应用启动时调用）。"""
    global _engine, _session_factory
    if _engine is not None:
        await dispose_database()
    _engine = create_engine(config)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


def get_engine() -> AsyncEngine:
    """返回已初始化的引擎。"""
    if _engine is None:
        raise RuntimeError("数据库尚未初始化，请先调用 init_database()")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """返回已初始化的会话工厂。"""
    if _session_factory is None:
        raise RuntimeError("数据库尚未初始化，请先调用 init_database()")
    return _session_factory


async def dispose_database() -> None:
    """释放引擎并清空全局状态。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


@contextlib.asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """脚本用会话：正常结束提交，异常回滚。"""
    session = get_session_factory()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def create_schema(engine: AsyncEngine | None = None) -> None:
    """按 ORM 元数据建表（仅测试与本地演练，生产用 Alembic 迁移）。

    建表后补一条自营租户：业务表的 `tenant_id` 非空且带外键，缺了它任何业务
    写入都会被外键挡下。迁移路径里由 Alembic 负责插同一条数据。
    """
    target = engine or get_engine()
    async with target.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_seed_self_tenant)
        await connection.run_sync(_seed_plan_templates)


def _seed_self_tenant(connection: Connection) -> None:
    """插入自营租户（幂等）。"""
    from app.db.models import SELF_TENANT_ID, SELF_TENANT_NAME

    connection.execute(
        text(
            "INSERT INTO tenants "
            "(id, name, kind, status, quota_type, quota_held, created_by, note, "
            "created_at, updated_at) "
            "SELECT :tenant_id, :name, 'self', 'active', 'none', 0, 'system', NULL, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
            "WHERE NOT EXISTS (SELECT 1 FROM tenants WHERE id = :tenant_id)"
        ),
        {"tenant_id": SELF_TENANT_ID, "name": SELF_TENANT_NAME},
    )


# 预置功能包模板：(code, name, kind, modules, limits)
_PLAN_TEMPLATE_SEEDS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "trial_carry",
        "1 天试用（搬运）",
        "trial",
        '["carry"]',
        '{"max_routes": 1, "allow_export": false}',
    ),
    (
        "trial_monitor",
        "1 天试用（监听）",
        "trial",
        '["monitor"]',
        '{"max_routes": 1, "allow_export": false}',
    ),
    ("standard", "常规开通", "standard", "[]", "{}"),
    ("full", "全功能", "standard", '["carry", "monitor", "discovery"]', "{}"),
)


def _seed_plan_templates(connection: Connection) -> None:
    """插入预置功能包模板（幂等，与迁移 0020 的 seed 保持一致）。"""
    for code, name, kind, modules, limits in _PLAN_TEMPLATE_SEEDS:
        connection.execute(
            text(
                "INSERT INTO plan_templates "
                "(code, name, kind, modules, limits, enabled, created_at, updated_at) "
                "SELECT :code, :name, :kind, :modules, :limits, 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
                "WHERE NOT EXISTS (SELECT 1 FROM plan_templates WHERE code = :code)"
            ),
            {
                "code": code,
                "name": name,
                "kind": kind,
                "modules": modules,
                "limits": limits,
            },
        )


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _register_sqlite_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()
