"""登录、登出与改密。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    bearer_token,
    build_user_identity,
    current_identity,
    session_dependency,
)
from app.api.schemas.auth import LoginRequest, PasswordChangeRequest
from app.core.config import AppConfig
from app.core.errors import AccountLockedError, AuthRequiredError, InvalidCredentialsError
from app.core.security import create_session_token, session_expiry
from app.db.base import as_utc, utc_now
from app.services import login_history_service, session_service, user_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# 身份契约字段：登录响应与 /api/auth/check 必须完全一致，避免前端两套解析
IDENTITY_FIELDS = (
    "username",
    "role",
    "account_type",
    "tenant_id",
    "tenant_status",
    "expires_at",
    "modules",
    "limits",
    "must_change_password",
    "is_builtin",
)


def identity_payload(identity: dict[str, Any]) -> dict[str, Any]:
    """从身份字典里挑出对外字段。"""
    return {key: identity.get(key) for key in IDENTITY_FIELDS}


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """账号密码登录，成功返回会话令牌。"""
    config: AppConfig = request.app.state.config
    username = payload.username.strip()
    user_agent = request.headers.get("user-agent")

    locked = await login_history_service.locked_until(
        session,
        username,
        window_minutes=config.security.login_window_minutes,
        max_failures=config.security.max_login_failures,
    )
    if locked is not None:
        await login_history_service.record_login_attempt(
            session,
            username=username,
            ip_address=_client_ip(request),
            user_agent=user_agent,
            success=False,
            reason="账号已锁定",
        )
        raise AccountLockedError(extra={"locked_until": as_utc(locked).isoformat()})

    user, reason = await user_service.authenticate(
        session,
        username=username,
        password=payload.password,
    )
    if user is None:
        await login_history_service.record_login_attempt(
            session,
            username=username,
            ip_address=_client_ip(request),
            user_agent=user_agent,
            success=False,
            reason=reason,
        )
        raise InvalidCredentialsError()

    token = create_session_token()
    expires_at = session_expiry(config)
    await session_service.create_web_session(
        session,
        token=token,
        username=user.username,
        role=user.role,
        expires_at=expires_at,
        ip_address=_client_ip(request),
    )
    user.last_login_at = utc_now()
    await session.commit()
    await login_history_service.record_login_attempt(
        session,
        username=user.username,
        ip_address=_client_ip(request),
        user_agent=user_agent,
        success=True,
    )

    # 登录响应与 /api/auth/check 共用同一套身份契约（含 account_type/modules/limits）。
    # 注意：`expires_at` 是**账号有效期**（会员到期日），会话令牌的过期时间单独叫
    # `session_expires_at`，两者不能混用——原先 `expires_at` 表示令牌过期，已改名。
    return {
        "token": token,
        "session_expires_at": as_utc(expires_at).isoformat(),
        **identity_payload(await build_user_identity(session, user)),
    }


@router.get("/check")
async def check(identity: dict[str, Any] = Depends(current_identity)) -> dict[str, Any]:
    """校验当前会话。"""
    return {"authenticated": True, **identity_payload(identity)}


@router.post("/logout")
async def logout(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(session_dependency),
    _: dict[str, Any] = Depends(current_identity),
) -> dict[str, bool]:
    """退出当前会话。"""
    token = bearer_token(authorization)
    if token:
        await session_service.revoke_web_session(session, token)
    return {"logged_out": True}


@router.post("/logout-all")
async def logout_all(
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """撤销本人全部会话。"""
    username = str(identity["username"])
    if username == "api-token":
        return {"username": username, "revoked": 0}
    count = await session_service.revoke_all_web_sessions(session, username)
    return {"username": username, "revoked": count}


@router.patch("/password")
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """修改本人密码；成功后其他设备会被下线。"""
    if identity.get("user_id") is None:
        raise AuthRequiredError("API Token 身份不支持修改密码")
    config: AppConfig = request.app.state.config
    user = await user_service.get_user(session, int(identity["user_id"]))
    if user is None:
        raise AuthRequiredError()

    await user_service.change_own_password(
        session,
        config,
        user=user,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    token = bearer_token(authorization)
    revoked = await session_service.revoke_other_sessions(session, user.username, token)
    return {"changed": True, "revoked_other_sessions": revoked}
