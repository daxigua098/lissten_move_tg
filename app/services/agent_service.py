"""代理的"我的下级"：递归子树、业绩统计与直属下级的启停（P3-05）。

规则（设计 §6）：

- **看**：上级能看整棵子树，不限层级；
- **改**：只能动**直属下级**（``parent_user_id = 当前账号``），隔层一律 403；
- 递归深度封顶 20 层，脏数据（成环）不会把查询拖死；
- 代理看不到任何下级会员的**业务数据**，列表只给账号与到期信息。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotDirectSubordinateError, NotFoundError
from app.db.base import as_utc
from app.db.models import (
    ACCOUNT_TYPE_AGENT,
    ACCOUNT_TYPE_MEMBER,
    ACTION_OPEN_AGENT,
    ACTION_OPEN_MEMBER,
    ACTION_OPEN_TRIAL,
    ACTION_RENEW_CONSUME,
    TENANT_KIND_MEMBER,
    TENANT_STATUS_ACTIVE,
    TENANT_STATUS_EXPIRED,
    TENANT_STATUS_SUSPENDED,
    AgentQuota,
    QuotaLedger,
    Tenant,
    User,
)
from app.services import quota_service, tenant_module_service

MAX_TREE_DEPTH = 20
# 到期预警窗口：3 天内到期算"即将到期"
EXPIRING_SOON_DAYS = 3

_SUBTREE_SQL = text(
    """
    WITH RECURSIVE subtree(id, depth) AS (
        SELECT id, 0 FROM users WHERE id = :root_id
        UNION ALL
        SELECT u.id, subtree.depth + 1
        FROM users AS u
        JOIN subtree ON u.parent_user_id = subtree.id
        WHERE subtree.depth < :max_depth
    )
    SELECT id, depth FROM subtree
    """
)


async def subtree_rows(session: AsyncSession, root_user_id: int) -> list[tuple[int, int]]:
    """返回 ``[(user_id, depth)]``；depth=0 是根自己。"""
    result = await session.execute(
        _SUBTREE_SQL,
        {"root_id": int(root_user_id), "max_depth": MAX_TREE_DEPTH},
    )
    return [(int(row[0]), int(row[1])) for row in result.all()]


async def subtree_ids(
    session: AsyncSession,
    root_user_id: int,
    *,
    include_self: bool = True,
) -> list[int]:
    """整棵子树的账号 ID（默认含自己）。"""
    rows = await subtree_rows(session, root_user_id)
    return [user_id for user_id, depth in rows if include_self or depth > 0]


async def get_direct_subordinate(
    session: AsyncSession,
    *,
    actor: User,
    user_id: int,
) -> User:
    """取直属下级；不存在 → 404，隔层/自己 → 403。"""
    if int(user_id) == actor.id:
        raise NotDirectSubordinateError("不能对自己执行该操作")
    target = await session.get(User, int(user_id))
    if target is None:
        raise NotFoundError("下级账号不存在")
    if target.parent_user_id != actor.id:
        raise NotDirectSubordinateError()
    return target


async def _tenants_by_owner(
    session: AsyncSession,
    user_ids: list[int],
) -> dict[int, Tenant]:
    """会员账号 → 它的租户（一个账号最多一个租户）。"""
    if not user_ids:
        return {}
    rows = await session.scalars(select(Tenant).where(Tenant.owner_user_id.in_(user_ids)))
    return {row.owner_user_id: row for row in rows if row.owner_user_id is not None}


async def _quotas_by_user(
    session: AsyncSession,
    user_ids: list[int],
) -> dict[int, AgentQuota]:
    if not user_ids:
        return {}
    rows = await session.scalars(select(AgentQuota).where(AgentQuota.user_id.in_(user_ids)))
    return {row.user_id: row for row in rows}


def _member_state(tenant: Tenant | None, *, now: datetime) -> dict[str, Any]:
    """会员租户的到期状态（P4 之前先在列表里算出来给前端显示）。"""
    if tenant is None:
        return {
            "tenant_id": None,
            "tenant_status": None,
            "expires_at": None,
            "quota_type": None,
            "expired": False,
            "days_left": None,
        }
    expires = as_utc(tenant.expires_at)
    days_left = None
    expired = False
    if expires is not None:
        delta = expires - now
        days_left = max(0, int(delta.total_seconds() // 86400))
        expired = delta.total_seconds() <= 0
    if tenant.status == TENANT_STATUS_SUSPENDED:
        state = TENANT_STATUS_SUSPENDED
    elif expired or tenant.status == TENANT_STATUS_EXPIRED:
        state = TENANT_STATUS_EXPIRED
    else:
        state = TENANT_STATUS_ACTIVE
    return {
        "tenant_id": tenant.id,
        "tenant_status": state,
        "expires_at": expires,
        "quota_type": tenant.quota_type,
        "quota_held": tenant.quota_held,
        "expired": expired,
        "days_left": days_left,
    }


def serialize_subordinate(
    user: User,
    *,
    depth: int,
    is_direct: bool,
    tenant: Tenant | None,
    quota: AgentQuota | None,
    now: datetime,
) -> dict[str, Any]:
    """下级列表的一行：账号 + 额度 + 到期，**不含任何业务数据**。"""
    payload: dict[str, Any] = {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "account_type": user.account_type,
        "role": user.role,
        "enabled": user.enabled,
        "parent_user_id": user.parent_user_id,
        "depth": depth,
        "is_direct": is_direct,
        "created_at": as_utc(user.created_at),
    }
    payload.update(_member_state(tenant, now=now))
    if user.account_type == ACCOUNT_TYPE_AGENT:
        payload["quota"] = quota_service.balances(quota)
    return payload


async def list_subordinates(
    session: AsyncSession,
    *,
    actor: User,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """整棵子树（不含自己），直属可操作、隔层只读。"""
    moment = now or datetime.now(UTC)
    rows = await subtree_rows(session, actor.id)
    children = [(user_id, depth) for user_id, depth in rows if depth > 0]
    if not children:
        return []
    ids = [user_id for user_id, _ in children]
    users = {user.id: user for user in await session.scalars(select(User).where(User.id.in_(ids)))}
    tenants = await _tenants_by_owner(session, ids)
    quotas = await _quotas_by_user(session, ids)

    items: list[dict[str, Any]] = []
    for user_id, depth in children:
        user = users.get(user_id)
        if user is None:  # 并发删除，跳过
            continue
        items.append(
            serialize_subordinate(
                user,
                depth=depth,
                is_direct=user.parent_user_id == actor.id,
                tenant=tenants.get(user_id),
                quota=quotas.get(user_id),
                now=moment,
            )
        )
    items.sort(key=lambda item: (item["depth"], item["id"]))
    return items


async def _owned_tenants(session: AsyncSession, ids: list[int]) -> list[Tenant]:
    """子树里所有归属这些账号的租户（代理开的 + 会员自己的）。"""
    if not ids:
        return []
    statement = select(Tenant).where(
        or_(Tenant.owner_agent_id.in_(ids), Tenant.owner_user_id.in_(ids))
    )
    return list(await session.scalars(statement))


async def agent_stats(
    session: AsyncSession,
    *,
    actor: User,
    now: datetime | None = None,
) -> dict[str, Any]:
    """业绩统计：本月开号、本月续期、活跃会员、即将到期、已过期。"""
    moment = now or datetime.now(UTC)
    month_start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    ids = await subtree_ids(session, actor.id)
    tenants = await _owned_tenants(session, ids)
    tenant_ids = [row.id for row in tenants]

    opened_this_month = 0
    renewed_this_month = 0
    ledger_statement = select(QuotaLedger.action, QuotaLedger.created_at).where(
        QuotaLedger.actor_user_id.in_(ids),
        QuotaLedger.action.in_(
            (
                ACTION_OPEN_MEMBER,
                ACTION_OPEN_TRIAL,
                ACTION_OPEN_AGENT,
                ACTION_RENEW_CONSUME,
            )
        ),
    )
    opened_actions = {ACTION_OPEN_MEMBER, ACTION_OPEN_TRIAL, ACTION_OPEN_AGENT}
    for action, created_at in (await session.execute(ledger_statement)).all():
        moment_created = as_utc(created_at) or moment
        if moment_created < month_start:
            continue
        if action in opened_actions:
            opened_this_month += 1
        else:
            renewed_this_month += 1

    active = 0
    expiring_soon = 0
    expired = 0
    for tenant in tenants:
        if tenant.kind != TENANT_KIND_MEMBER:
            continue
        state = _member_state(tenant, now=moment)
        if state["tenant_status"] == TENANT_STATUS_SUSPENDED:
            continue
        if state["expired"]:
            expired += 1
            continue
        active += 1
        days_left = state["days_left"]
        if days_left is not None and days_left <= EXPIRING_SOON_DAYS:
            expiring_soon += 1

    return {
        "subtree_users": len(ids),
        "tenants": len(tenant_ids),
        "opened_this_month": opened_this_month,
        "renewed_this_month": renewed_this_month,
        "active_members": active,
        "expiring_soon": expiring_soon,
        "expired": expired,
        "month_start": month_start,
        "generated_at": moment,
    }


async def expiring_tenants(
    session: AsyncSession,
    *,
    actor: User,
    days: int = EXPIRING_SOON_DAYS,
    now: datetime | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """名下即将到期 / 已过期的客户，用于工作台顶部的到期预警。"""
    moment = now or datetime.now(UTC)
    horizon = moment + timedelta(days=int(days))
    ids = await subtree_ids(session, actor.id)
    tenants = await _owned_tenants(session, ids)
    owners = await _tenants_owner_users(session, tenants)

    items: list[dict[str, Any]] = []
    for tenant in tenants:
        if tenant.kind != TENANT_KIND_MEMBER:
            continue
        expires = as_utc(tenant.expires_at)
        if expires is None or expires > horizon:
            continue
        owner = owners.get(tenant.owner_user_id)
        payload = _member_state(tenant, now=moment)
        payload.update(
            {
                "tenant_id": tenant.id,
                "tenant_name": tenant.name,
                "username": None if owner is None else owner.username,
                "enabled": None if owner is None else owner.enabled,
            }
        )
        items.append(payload)
    items.sort(key=lambda item: item["expires_at"] or moment)
    return items[:limit]


async def _tenants_owner_users(
    session: AsyncSession,
    tenants: list[Tenant],
) -> dict[int, User]:
    owner_ids = [row.owner_user_id for row in tenants if row.owner_user_id is not None]
    if not owner_ids:
        return {}
    rows = await session.scalars(select(User).where(User.id.in_(owner_ids)))
    return {row.id: row for row in rows}


async def sum_held_quota(session: AsyncSession, *, actor: User) -> dict[str, int]:
    """「已开出去还没到期」占用的额度数——用来提示"请预留 N 个额度"。"""
    ids = await subtree_ids(session, actor.id)
    counts = {"member": 0, "trial": 0}
    if not ids:
        return counts
    rows = await session.scalars(
        select(Tenant).where(
            Tenant.owner_agent_id.in_(ids),
            Tenant.kind == TENANT_KIND_MEMBER,
        )
    )
    for tenant in rows:
        if not tenant.quota_held:
            continue
        if tenant.quota_type in counts:
            counts[tenant.quota_type] += 1
    return counts


async def module_summary(session: AsyncSession, tenant_id: int | None) -> list[str]:
    """某个会员租户的功能块显示名（列表里的"功能包"列）。"""
    if tenant_id is None:
        return []
    modules = await tenant_module_service.list_modules(session, tenant_id)
    return tenant_module_service.module_labels(modules)


async def set_subordinate_enabled(
    session: AsyncSession,
    *,
    actor: User,
    user_id: int,
    enabled: bool,
    suspend_tenant: bool = True,
) -> dict[str, Any]:
    """停用 / 解停直属下级。

    停用会员时顺手把租户置为 ``suspended``（P4 的服务端守卫据此拒绝写操作），
    解停时若已过期则维持 ``expired``，否则回到 ``active``。设置与数据都保留，
    只是功能被关掉——会员登录后需要自己手动启动线路才会恢复。
    """
    target = await get_direct_subordinate(session, actor=actor, user_id=user_id)
    target.enabled = bool(enabled)
    tenant = None
    if target.account_type == ACCOUNT_TYPE_MEMBER and target.tenant_id is not None:
        tenant = await session.get(Tenant, target.tenant_id)
    if tenant is not None and suspend_tenant:
        if not enabled:
            tenant.status = TENANT_STATUS_SUSPENDED
        else:
            expired = _member_state(tenant, now=datetime.now(UTC))["expired"]
            tenant.status = TENANT_STATUS_EXPIRED if expired else TENANT_STATUS_ACTIVE
    await session.flush()
    await session.commit()
    await session.refresh(target)
    if tenant is not None:
        await session.refresh(tenant)
    return {
        "account": serialize_subordinate(
            target,
            depth=1,
            is_direct=True,
            tenant=tenant,
            quota=None,
            now=datetime.now(UTC),
        ),
        "revoked_sessions": 0 if enabled else -1,
    }
