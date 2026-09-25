"""领域异常与错误响应测试。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.errors import (
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
    register_exception_handlers,
)


@pytest.mark.parametrize(
    ("error_class", "status_code", "code"),
    [
        (ValidationFailedError, 400, "VALIDATION_ERROR"),
        (PasswordWeakError, 400, "AUTH_PASSWORD_WEAK"),
        (BuiltinPasswordEnvError, 400, "AUTH_BUILTIN_PASSWORD_ENV"),
        (AuthRequiredError, 401, "AUTH_REQUIRED"),
        (InvalidCredentialsError, 401, "AUTH_INVALID_CREDENTIALS"),
        (PasswordChangeRequiredError, 403, "AUTH_PASSWORD_CHANGE_REQUIRED"),
        (PermissionDeniedError, 403, "PERMISSION_DENIED"),
        (NotFoundError, 404, "NOT_FOUND"),
        (ConflictError, 409, "CONFLICT"),
        (UserExistsError, 409, "USER_EXISTS"),
        (LastSuperAdminError, 409, "USER_LAST_SUPER_ADMIN"),
        (SelfOperationError, 409, "USER_SELF_DELETE"),
        (AccountLockedError, 423, "AUTH_LOCKED"),
        (RateLimitedError, 429, "RATE_LIMITED"),
    ],
)
def test_error_contract(error_class, status_code, code) -> None:
    error = error_class()

    assert error.status_code == status_code
    assert error.code == code
    assert error.payload() == {"detail": error.default_detail, "code": code}


def test_base_error_defaults_to_internal_error() -> None:
    error = AppError()

    assert error.status_code == 500
    assert error.code == "INTERNAL_ERROR"
    assert error.payload()["detail"] == "服务内部错误"


def test_error_accepts_overrides_and_extra() -> None:
    error = AccountLockedError("已锁定", extra={"locked_until": "2026-09-25T15:00:00+08:00"})

    payload = error.payload()

    assert payload["detail"] == "已锁定"
    assert payload["code"] == "AUTH_LOCKED"
    assert payload["locked_until"] == "2026-09-25T15:00:00+08:00"


def test_custom_status_and_code_override() -> None:
    error = AppError("自定义", code="CUSTOM", status_code=418)

    assert error.status_code == 418
    assert error.code == "CUSTOM"


def test_register_exception_handlers_returns_json() -> None:
    application = FastAPI()
    register_exception_handlers(application)

    @application.get("/boom")
    async def boom() -> None:
        raise PermissionDeniedError("无权访问")

    async def call() -> tuple[int, dict]:
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/boom")
        return response.status_code, response.json()

    status_code, body = asyncio.run(call())

    assert status_code == 403
    assert body == {"detail": "无权访问", "code": "PERMISSION_DENIED"}
