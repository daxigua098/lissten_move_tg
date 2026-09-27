"""请求上下文、缓存头、租户作用域与写操作审计。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from loguru import logger

from app.db.session import get_session_factory
from app.db.tenant_context import reset_tenant_scope, set_tenant_scope
from app.services import audit_service

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class TenantScopeMiddleware:
    """纯 ASGI 中间件：每个请求的租户作用域用完即还原（P1-05）。

    作用域本身由 ``current_identity`` 按登录身份设定；这里只负责把请求开头的
    值存下来、请求结束后还原，保证请求之间（以及测试里的直接 service 调用）
    互相不串。写成纯 ASGI 而不是 BaseHTTPMiddleware，是为了不在请求里多起一层
    任务，作用域设定与业务查询落在同一个上下文里。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        token = set_tenant_scope(None)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_tenant_scope(token)


def register_middlewares(app: FastAPI) -> None:
    """注册请求 ID、缓存头、租户作用域与审计中间件。"""
    # 先注册的在内层：租户作用域要贴着路由，才能和业务查询同处一个上下文
    app.add_middleware(TenantScopeMiddleware)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        request.state.request_id = uuid4().hex[:12]
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        path = request.url.path
        if path in {"/", "/index.html"}:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        elif path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @app.middleware("http")
    async def audit_middleware(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        if request.method in WRITE_METHODS and request.url.path.startswith("/api/"):
            identity = getattr(request.state, "identity", None)
            username = str(identity.get("username")) if identity else "anonymous"
            try:
                factory = get_session_factory()
                async with factory() as session:
                    await audit_service.write_audit_log(
                        session,
                        username=username,
                        method=request.method,
                        path=request.url.path,
                        status_code=response.status_code,
                        ip_address=request.client.host if request.client else None,
                    )
            except Exception as exc:  # noqa: BLE001 - 审计失败不能影响业务响应
                logger.warning("审计日志写入失败：{}", exc)
        return response
