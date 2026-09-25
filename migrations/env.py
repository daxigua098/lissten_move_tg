"""Alembic 迁移环境：数据库地址取自项目配置，支持异步引擎。

地址解析优先级：命令行 `-x db_url=...` > ini 中的 `sqlalchemy.url`（由 CLI 注入）> 项目配置。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from pathlib import Path
from typing import Any

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import load_config
from app.db import models as _models  # noqa: F401  导入以注册全部模型
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _x_arguments() -> dict[str, str]:
    return context.get_x_argument(as_dictionary=True) or {}


def _database_url() -> str:
    xargs = _x_arguments()
    if xargs.get("db_url"):
        return xargs["db_url"]
    from_ini = config.get_main_option("sqlalchemy.url")
    if from_ini:
        return from_ini
    return load_config(project_root=xargs.get("project_root")).database.url


def _ensure_sqlite_directory(url: str) -> None:
    """SQLite 文件所在目录不存在时自动创建。

    迁移路径直接创建引擎，不走 app.db.session.create_engine，所以这里要单独保证
    目录存在，否则首次部署会报 unable to open database file。
    """
    if not url.startswith("sqlite") or ":///" not in url:
        return
    raw = url.partition(":///")[2].split("?", 1)[0]
    if raw in {"", ":memory:"}:
        return
    Path(raw).expanduser().parent.mkdir(parents=True, exist_ok=True)


def _configure(connection: Connection | None = None, *, url: str | None = None) -> None:
    options: dict[str, Any] = {
        "target_metadata": target_metadata,
        "compare_type": True,
    }
    if connection is not None:
        options["connection"] = connection
        # SQLite 不支持直接 ALTER，后续结构变更需要 batch 模式
        options["render_as_batch"] = connection.dialect.name == "sqlite"
    else:
        options["url"] = url
        options["literal_binds"] = True
        options["dialect_opts"] = {"paramstyle": "named"}
    context.configure(**options)


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连接数据库。"""
    _configure(url=_database_url())
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    url = _database_url()
    _ensure_sqlite_directory(url)
    engine = create_async_engine(url, poolclass=pool.NullPool, future=True)
    async with engine.connect() as connection:
        await connection.run_sync(_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    """在线模式：连接数据库执行迁移。"""
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
