"""FastAPI 应用装配。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.api.errors import register_exception_handlers
from app.api.middleware import register_middlewares
from app.api.routers import (
    accounts,
    auth,
    bots,
    health,
    logs,
    sources,
    system,
    targets,
    users,
)
from app.core.config import AppConfig, load_config
from app.db.session import dispose_database, get_session_factory, init_database
from app.services import user_service


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时初始化数据库，关闭时释放连接。"""
    config: AppConfig = app.state.config
    await init_database(config)
    try:
        await _warn_if_no_super_admin()
        yield
    finally:
        await dispose_database()


async def _warn_if_no_super_admin() -> None:
    factory = get_session_factory()
    async with factory() as session:
        count = await user_service.count_active_super_admins(session)
    if count == 0:
        logger.warning("系统中没有启用的超级管理员，请执行：python main.py create-admin")


def create_app(
    config: AppConfig | None = None,
    *,
    bot_client_factory: Any = None,
    account_client_factory: Any = None,
) -> FastAPI:
    """构造应用实例。

    两个 factory 用于测试注入 Telethon 替身；生产传 None 走真实调用。
    """
    resolved = config or load_config()
    app = FastAPI(
        title="TG 线索运营系统",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.config = resolved
    app.state.bot_client_factory = bot_client_factory
    app.state.account_client_factory = account_client_factory
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.server.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_middlewares(app)
    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(logs.router)
    app.include_router(system.router)
    app.include_router(accounts.router)
    app.include_router(bots.router)
    app.include_router(sources.router)
    app.include_router(targets.router)

    mount_frontend(app, resolved)
    return app


def mount_frontend(app: FastAPI, config: AppConfig) -> None:
    """前端构建产物存在时挂载为静态站点（同域，无需额外反代）。"""
    dist = config.project_root / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    else:
        logger.info("未找到前端构建产物 {}，仅提供 API。", dist)
