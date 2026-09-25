"""HTTP 层的错误响应：领域异常在此统一注册，并对外重新导出。"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.core.errors import (
    AccountLockedError,
    AppError,
    AuthRequiredError,
    BuiltinPasswordEnvError,
    ConflictError,
    InvalidCredentialsError,
    LastSuperAdminError,
    NotFoundError,
    PasswordChangeRequiredError,
    PasswordWeakError,
    PermissionDeniedError,
    RateLimitedError,
    SelfOperationError,
    UserExistsError,
    ValidationFailedError,
)

__all__ = [
    "AccountLockedError",
    "AppError",
    "AuthRequiredError",
    "BuiltinPasswordEnvError",
    "ConflictError",
    "InvalidCredentialsError",
    "LastSuperAdminError",
    "NotFoundError",
    "PasswordChangeRequiredError",
    "PasswordWeakError",
    "PermissionDeniedError",
    "RateLimitedError",
    "SelfOperationError",
    "UserExistsError",
    "ValidationFailedError",
    "register_exception_handlers",
]


def register_exception_handlers(app: Any) -> None:
    """把领域异常注册为统一 JSON 错误响应。"""
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.exception_handler(AppError)
    async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.payload())

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理的异常：{}", exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "服务内部错误", "code": "INTERNAL_ERROR"},
        )
