"""鉴权依赖：会话或 API Token → 身份字典 → 角色与功能块校验。

三类账号共用同一张登录页，靠身份字典里的 ``account_type`` 分流：

- ``platform``：平台自用账号，不受功能块限制；
- ``agent``：代理账号，**纯开号面板**，访问任何业务接口一律 403；
- ``member``：会员账号，按 ``tenant_modules`` 逐块放行。

功能块之外还有一层"基础能力"（登录、改密、TG 账号、机器人、运行总览），
对所有会员恒开，用 ``require_member_or_platform`` 表达。
"""

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
    TenantExpiredError,
)
from app.core.security import compare_token
from app.db.base import as_utc
from app.db.models import (
    ACCOUNT_TYPE_AGENT,
    ACCOUNT_TYPE_MEMBER,
    ACCOUNT_TYPE_PLATFORM,
    MODULES,
    ROLE_RANK,
    ROLE_SUB_ADMIN,
    ROLE_SUPER_ADMIN,
    SELF_TENANT_ID,
    TENANT_STATUS_ACTIVE,
    User,
)
from app.db.session import get_session_factory
from app.db.tenant_context import set_tenant_scope
from app.services import (
    session_service,
    tenant_module_service,
    tenant_service,
    tenant_status_service,
    user_service,
)

# 强制改密期间仍然允许访问的接口
PASSWORD_CHANGE_ALLOWED_PATHS = {
    "/api/auth/password",
    "/api/auth/logout",
    "/api/auth/logout-all",
    "/api/auth/check",
}

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 账号过期 / 停用后仍然允许的写操作：
# - ``/api/auth/*``：登录、改密、登出，否则用户进不来也出不去；
# - ``/api/agent/*``：代理给过期客户续期、解停、划拨（会员走不到这里，代理身份不受本守卫约束）；
# - 导出一律是 GET（``/api/leads/export.csv``），所以不需要额外白名单。
EXPIRY_WRITE_ALLOWED_PREFIXES = ("/api/auth/",)


def expiry_write_allowed(path: str) -> bool:
    """过期 / 停用状态下仍然放行的写路径。"""
    return path.startswith(EXPIRY_WRITE_ALLOWED_PREFIXES)


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


def _platform_identity(username: str, *, user_id: int | None) -> dict[str, Any]:
    """平台账号身份：不受功能块限制，返回全部功能块。"""
    return {
        "username": username,
        "role": ROLE_SUPER_ADMIN,
        "must_change_password": False,
        "is_builtin": False,
        "user_id": user_id,
        "account_type": ACCOUNT_TYPE_PLATFORM,
        "tenant_id": None,
        "tenant_status": TENANT_STATUS_ACTIVE,
        "expires_at": None,
        "modules": list(MODULES),
        "limits": {},
    }


async def _member_context(session: AsyncSession, user: User) -> dict[str, Any]:
    """会员账号的租户上下文：状态、有效期、功能块与用量限制。"""
    context: dict[str, Any] = {
        "tenant_status": TENANT_STATUS_ACTIVE,
        "expires_at": None,
        "modules": [],
        "limits": {},
    }
    if user.tenant_id is None:
        return context
    tenant = await tenant_service.get_tenant(session, user.tenant_id)
    if tenant is not None:
        # P4-01：状态**现算**，不看数据库里那个可能滞后的 status 字段
        context["tenant_status"] = tenant_status_service.effective_status(tenant)
        expires = as_utc(tenant.expires_at)
        context["expires_at"] = expires.isoformat() if expires else None
        context["tenant_runtime_enabled"] = bool(tenant.runtime_enabled)
        context["tenant_stop_reason"] = tenant.runtime_stop_reason
    context["modules"] = await tenant_module_service.list_modules(session, user.tenant_id)
    limit = await tenant_module_service.get_limits(session, user.tenant_id)
    context["limits"] = tenant_module_service.limits_to_payload(limit)
    return context


async def build_user_identity(session: AsyncSession, user: User) -> dict[str, Any]:
    """把账号行展开成完整身份字典。"""
    identity: dict[str, Any] = {
        "username": user.username,
        "role": user.role,
        "must_change_password": user.must_change_password,
        "is_builtin": user.is_builtin,
        "user_id": user.id,
        "account_type": user.account_type,
        "tenant_id": user.tenant_id,
        "tenant_status": TENANT_STATUS_ACTIVE,
        "expires_at": None,
        "modules": list(MODULES) if user.account_type == ACCOUNT_TYPE_PLATFORM else [],
        "limits": {},
    }
    if user.account_type == ACCOUNT_TYPE_MEMBER:
        identity.update(await _member_context(session, user))
    return identity


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
        identity = _platform_identity("api-token", user_id=None)
    elif token:
        record = await session_service.find_web_session(session, token)
        if record is not None:
            # 角色以数据库当前值为准，改权限后立即生效
            user = await user_service.get_user_by_username(session, record.username)
            if user is not None and user.enabled:
                identity = await build_user_identity(session, user)

    if identity is None:
        raise AuthRequiredError()
    # P4-02：账号过期 / 停用后只能看，写操作一律 403（白名单除外）。
    # 判定放在这里，所有走身份依赖的接口自动生效，不会散落到各路由漏网。
    if (
        identity["account_type"] == ACCOUNT_TYPE_MEMBER
        and identity["tenant_status"] != TENANT_STATUS_ACTIVE
        and request.method in WRITE_METHODS
        and not expiry_write_allowed(request.url.path)
    ):
        raise TenantExpiredError(extra={"tenant_status": identity["tenant_status"]})
    # 内置管理员的密码在服务器 .env 管理，不参与网页强制改密（否则会死锁进不去）
    if (
        identity["must_change_password"]
        and not identity["is_builtin"]
        and request.url.path not in PASSWORD_CHANGE_ALLOWED_PATHS
    ):
        raise PasswordChangeRequiredError()

    request.state.identity = identity
    # P1-05：按身份设请求级租户作用域，业务表的读写都由它收口
    set_tenant_scope(tenant_scope_of(identity))
    return identity


def _account_type(identity: dict[str, Any]) -> str:
    """取账号类型；缺省按平台账号处理（兼容仅给了角色的旧身份）。"""
    return str(identity.get("account_type") or ACCOUNT_TYPE_PLATFORM)


def tenant_scope_of(identity: dict[str, Any]) -> int:
    """这个请求该落在哪个租户（P1-05）。

    会员用自己的租户；平台账号与 API Token 用自营租户——平台管通道与客户，
    日常操作（执行账号、线路、词库、线索）都发生在自营业务上。
    """
    value = identity.get("tenant_id")
    return int(value) if value is not None else SELF_TENANT_ID


def require_role(minimum: str):
    """生成角色守卫依赖：等级不足即 403（只在平台账号内部继续使用）。"""
    required = ROLE_RANK[minimum]

    async def checker(identity: dict[str, Any] = Depends(current_identity)) -> None:
        if ROLE_RANK.get(str(identity.get("role")), 0) < required:
            raise PermissionDeniedError()

    return checker


async def require_platform(identity: dict[str, Any] = Depends(current_identity)) -> None:
    """仅平台账号可访问（平台后台全部接口）。"""
    if _account_type(identity) != ACCOUNT_TYPE_PLATFORM:
        raise PermissionDeniedError()


async def require_agent_or_platform(
    identity: dict[str, Any] = Depends(current_identity),
) -> None:
    """代理工作台接口：代理账号或平台账号可访问。"""
    if _account_type(identity) not in (ACCOUNT_TYPE_AGENT, ACCOUNT_TYPE_PLATFORM):
        raise PermissionDeniedError()


def _platform_allows(identity: dict[str, Any], minimum_role: str | None) -> bool:
    """平台账号沿用原有角色等级；``None`` 表示不设门槛。"""
    if minimum_role is None:
        return True
    return ROLE_RANK.get(str(identity.get("role")), 0) >= ROLE_RANK[minimum_role]


def require_member_or_platform(minimum_role: str | None = ROLE_SUB_ADMIN):
    """生成「基础能力」守卫：平台账号或会员账号可访问，代理账号一律 403。

    基础能力 = 登录 / 改密 / TG 账号 / 机器人 / 运行总览 / 线路骨架。
    平台账号在这里**仍然沿用内部角色分级**（默认 sub_admin 起），
    否则平台自己的 viewer 会因为"功能块概念不适用"而拿到额外权限。
    """

    async def checker(identity: dict[str, Any] = Depends(current_identity)) -> None:
        account_type = _account_type(identity)
        if account_type == ACCOUNT_TYPE_AGENT:
            raise PermissionDeniedError()
        if account_type == ACCOUNT_TYPE_PLATFORM and not _platform_allows(identity, minimum_role):
            raise PermissionDeniedError()

    return checker


def require_module(module: str, minimum_role: str | None = ROLE_SUB_ADMIN):
    """生成功能块守卫：平台账号按角色放行，代理 403，会员查 ``tenant_modules``。"""

    async def checker(identity: dict[str, Any] = Depends(current_identity)) -> None:
        account_type = _account_type(identity)
        if account_type == ACCOUNT_TYPE_PLATFORM:
            if not _platform_allows(identity, minimum_role):
                raise PermissionDeniedError()
            return
        if account_type == ACCOUNT_TYPE_AGENT:
            raise PermissionDeniedError()
        if module not in identity.get("modules", []):
            raise PermissionDeniedError()

    return checker


async def current_user_id(identity: dict[str, Any] = Depends(current_identity)) -> int | None:
    """当前操作者 ID；API Token 调用时为 None。"""
    value = identity.get("user_id")
    return int(value) if value is not None else None
