"""FastAPI 应用装配。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.api.errors import register_exception_handlers
from app.api.middleware import register_middlewares
from app.api.routers import (
    accounts,
    ad_assets,
    auth,
    bots,
    health,
    hot_keywords,
    jobs,
    keywords,
    leads,
    logs,
    meta,
    resources,
    routes,
    runtime,
    sources,
    system,
    targets,
    uploads,
    users,
)
from app.core.account_client_pool import close_account_clients
from app.core.config import AppConfig, load_config
from app.core.demo_client import demo_account_client_factory, demo_bot_client_factory
from app.core.paths import ensure_dir
from app.db.session import dispose_database, get_session_factory, init_database
from app.services import user_service
from app.services.upload_service import uploads_directory


class HashedAssetFiles(StaticFiles):
    """构建产物（文件名带哈希）：可以放心强缓存一年。

    入口 index.html 必须每次校验，否则浏览器会一直用旧的 index.html 去加载
    早就不存在的旧 bundle——改了功能用户也看不到。
    """

    def file_response(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时初始化数据库，关闭时释放连接。"""
    config: AppConfig = app.state.config
    await init_database(config)
    try:
        await _warn_if_no_super_admin()
        yield
    finally:
        # 池化过执行账号连接，退出前要断开
        await close_account_clients()
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
    directory_fetcher_factory: Any = None,
) -> FastAPI:
    """构造应用实例。

    三个 factory 用于测试注入替身（Telethon 账号 / 控制机器人 / 目录站抓取）；
    生产传 None 走真实调用。
    """
    resolved = config or load_config()
    app = FastAPI(
        title="TG 线索运营系统",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.config = resolved
    if resolved.app.demo_mode:
        logger.warning("本地演练模式已开启：不会连接 Telegram，转发与发送只写日志")
        bot_client_factory = bot_client_factory or demo_bot_client_factory()
        account_client_factory = account_client_factory or demo_account_client_factory()
    app.state.bot_client_factory = bot_client_factory
    app.state.account_client_factory = account_client_factory
    app.state.directory_fetcher_factory = directory_fetcher_factory
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
    app.include_router(meta.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(logs.router)
    app.include_router(system.router)
    app.include_router(accounts.router)
    app.include_router(bots.router)
    app.include_router(sources.router)
    app.include_router(targets.router)
    app.include_router(routes.router)
    app.include_router(ad_assets.router)
    app.include_router(keywords.router)
    app.include_router(keywords.keyword_router)
    app.include_router(leads.router)
    app.include_router(hot_keywords.router)
    app.include_router(resources.router)
    app.include_router(uploads.router)
    app.include_router(runtime.router)
    app.include_router(jobs.router)
    app.mount(
        "/uploads",
        StaticFiles(directory=ensure_dir(uploads_directory(resolved))),
        name="uploads",
    )

    mount_frontend(app, resolved)
    return app


def mount_frontend(app: FastAPI, config: AppConfig) -> None:
    """挂载前端构建产物，并为前端路由提供 SPA 回退。

    只挂 StaticFiles 的话，浏览器刷新 `/routes` 这类前端路由会拿到
    `{"detail":"Not Found"}`——因为磁盘上没有对应文件。这里加一条兜底路由：
    能命中的静态文件照常返回，其余非 API 路径统一回 index.html。
    """
    dist = config.project_root / "frontend" / "dist"
    if not dist.is_dir():
        logger.info("未找到前端构建产物 {}，仅提供 API。", dist)
        return

    assets_dir = dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", HashedAssetFiles(directory=assets_dir), name="assets")

    index_file = dist / "index.html"
    resolved_dist = dist.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        """前端路由回退：非 API 路径一律返回入口页面。"""
        if full_path.startswith(("api/", "uploads/", "assets/")) or full_path == "health":
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = (dist / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(resolved_dist):
            return FileResponse(candidate, headers={"Cache-Control": "no-cache"})
        if not index_file.is_file():
            raise HTTPException(status_code=404, detail="前端尚未构建")
        return FileResponse(index_file, headers={"Cache-Control": "no-cache"})
