"""请求上下文、缓存头与写操作审计。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from loguru import logger

from app.db.session import get_session_factory
from app.services import audit_service

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def register_middlewares(app: FastAPI) -> None:
    """注册请求 ID、缓存头与审计中间件。"""

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
