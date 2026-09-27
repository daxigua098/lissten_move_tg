"""租户业务逻辑：自营租户保底、查询与开通（P1 多租户地基）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.db.models import (
    SELF_TENANT_ID,
    SELF_TENANT_NAME,
    TENANT_KIND_MEMBER,
    TENANT_KIND_SELF,
    TENANT_KINDS,
    TENANT_STATUS_ACTIVE,
    Tenant,
)

TENANT_NAME_MAX_LENGTH = 64


def validate_tenant_name(name: str) -> str:
    """校验并规范化租户名。"""
    value = (name or "").strip()
    if not value:
        raise ValidationFailedError("租户名不能为空")
    if len(value) > TENANT_NAME_MAX_LENGTH:
        raise ValidationFailedError(f"租户名最长 {TENANT_NAME_MAX_LENGTH} 个字符")
    return value


async def get_tenant(session: AsyncSession, tenant_id: int) -> Tenant | None:
    """按 ID 查询租户。"""
    return await session.get(Tenant, tenant_id)


async def get_tenant_by_name(session: AsyncSession, name: str) -> Tenant | None:
    """按租户名查询（名字全局唯一）。"""
    return await session.scalar(select(Tenant).where(Tenant.name == (name or "").strip()))


async def get_tenant_by_owner(session: AsyncSession, user_id: int) -> Tenant | None:
    """按登录账号查询其租户（会员账号与租户 1:1）。"""
    return await session.scalar(select(Tenant).where(Tenant.owner_user_id == user_id))


async def count_tenants(
    session: AsyncSession,
    *,
    kind: str | None = None,
    status: str | None = None,
) -> int:
    """统计租户数量。"""
    statement = select(func.count()).select_from(Tenant)
    if kind is not None:
        statement = statement.where(Tenant.kind == kind)
    if status is not None:
        statement = statement.where(Tenant.status == status)
    return int(await session.scalar(statement) or 0)


async def list_tenants(
    session: AsyncSession,
    *,
    limit: int = 20,
    offset: int = 0,
    kind: str | None = None,
    status: str | None = None,
) -> tuple[list[Tenant], int]:
    """分页查询租户列表。"""
    statement = select(Tenant).order_by(Tenant.id)
    count_statement = select(func.count()).select_from(Tenant)
    if kind is not None:
        statement = statement.where(Tenant.kind == kind)
        count_statement = count_statement.where(Tenant.kind == kind)
    if status is not None:
        statement = statement.where(Tenant.status == status)
        count_statement = count_statement.where(Tenant.status == status)
    rows = list(await session.scalars(statement.limit(limit).offset(offset)))
    total = int(await session.scalar(count_statement) or 0)
    return rows, total


async def create_tenant(
    session: AsyncSession,
    *,
    name: str,
    kind: str = TENANT_KIND_MEMBER,
    owner_user_id: int | None = None,
    expires_at: datetime | None = None,
    created_by: str | None = None,
    note: str | None = None,
    status: str = TENANT_STATUS_ACTIVE,
) -> Tenant:
    """开通租户。

    `kind='member'` 必须带 `owner_user_id`（一个登录账号最多属于一个租户，
    由 `tenants.owner_user_id` 唯一约束兜底）。
    """
    if kind not in TENANT_KINDS:
        raise ValidationFailedError(f"租户类型必须是 {'/'.join(TENANT_KINDS)} 之一")
    value = validate_tenant_name(name)
    if await get_tenant_by_name(session, value) is not None:
        raise ConflictError("租户名已存在")
    if kind == TENANT_KIND_MEMBER:
        if owner_user_id is None:
            raise ValidationFailedError("会员租户必须绑定登录账号")
        if await get_tenant_by_owner(session, owner_user_id) is not None:
            raise ConflictError("该账号已经开通了租户")

    tenant = Tenant(
        name=value,
        kind=kind,
        status=status,
        owner_user_id=owner_user_id,
        expires_at=expires_at,
        created_by=(created_by or "").strip() or None,
        note=(note or "").strip() or None,
    )
    session.add(tenant)
    await session.commit()
    await session.refresh(tenant)
    return tenant


async def ensure_self_tenant(session: AsyncSession) -> Tenant:
    """确保自营租户存在（幂等）。

    `id=1` 由迁移插入；本地演练或测试直接用 `create_schema()` 建表时
    需要这个保底，否则存量数据没有归属。
    """
    existing = await get_tenant(session, SELF_TENANT_ID)
    if existing is not None:
        return existing
    tenant = Tenant(
        id=SELF_TENANT_ID,
        name=SELF_TENANT_NAME,
        kind=TENANT_KIND_SELF,
        status=TENANT_STATUS_ACTIVE,
        created_by="system",
    )
    session.add(tenant)
    await session.commit()
    await session.refresh(tenant)
    return tenant


async def require_tenant(session: AsyncSession, tenant_id: int) -> Tenant:
    """按 ID 取租户，不存在即报错（路由层与后台用）。"""
    tenant = await get_tenant(session, tenant_id)
    if tenant is None:
        raise NotFoundError("租户不存在")
    return tenant
