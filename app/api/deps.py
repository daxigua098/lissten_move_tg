"""鉴权依赖：会话或 API Token → 身份字典 → 角色校验。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.errors import (
    AuthRequiredError,
    PasswordChangeRequiredError,
    PermissionDeniedError,
)
from app.core.security import compare_token
from app.db.models import ROLE_RANK, ROLE_SUPER_ADMIN
from app.db.session import get_session_factory
from app.services import session_service, user_service

# 强制改密期间仍然允许访问的接口
PASSWORD_CHANGE_ALLOWED_PATHS = {
    "/api/auth/password",
    "/api/auth/logout",
    "/api/auth/logout-all",
    "/api/auth/check",
}


async def session_dependency() -> AsyncIterator[AsyncSession]:
    """每个请求一个会话；事务由 services 层提交。"""
    factory = get_session_factory()
    async with factory() as session:
        yield session


def bearer_token(authorization: str | None) -> str:
    """从 Authorization 头取出 Bearer 令牌。"""
    if not authorization or not authorization.startswith("Bearer "):
        return ""
    return authorization[len("Bearer ") :].strip()


async def current_identity(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """解析当前身份：API Token 或服务端会话，并执行强制改密拦截。"""
    config: AppConfig = request.app.state.config
    token = bearer_token(authorization)
    identity: dict[str, Any] | None = None

    if (
        token
        and config.secrets.admin_api_token
        and compare_token(token, config.secrets.admin_api_token)
    ):
        identity = {
            "username": "api-token",
            "role": ROLE_SUPER_ADMIN,
            "must_change_password": False,
            "is_builtin": False,
            "user_id": None,
        }
    elif token:
        record = await session_service.find_web_session(session, token)
        if record is not None:
            # 角色以数据库当前值为准，改权限后立即生效
            user = await user_service.get_user_by_username(session, record.username)
            if user is not None and user.enabled:
                identity = {
                    "username": user.username,
                    "role": user.role,
                    "must_change_password": user.must_change_password,
                    "is_builtin": user.is_builtin,
                    "user_id": user.id,
                }

    if identity is None:
        raise AuthRequiredError()
    # 内置管理员的密码在服务器 .env 管理，不参与网页强制改密（否则会死锁进不去）
    if (
        identity["must_change_password"]
        and not identity["is_builtin"]
        and request.url.path not in PASSWORD_CHANGE_ALLOWED_PATHS
    ):
        raise PasswordChangeRequiredError()

    request.state.identity = identity
    return identity


def require_role(minimum: str):
    """生成角色守卫依赖：等级不足即 403。"""
    required = ROLE_RANK[minimum]

    async def checker(identity: dict[str, Any] = Depends(current_identity)) -> None:
        if ROLE_RANK.get(str(identity.get("role")), 0) < required:
            raise PermissionDeniedError()

    return checker


async def current_user_id(identity: dict[str, Any] = Depends(current_identity)) -> int | None:
    """当前操作者 ID；API Token 调用时为 None。"""
    value = identity.get("user_id")
    return int(value) if value is not None else None
