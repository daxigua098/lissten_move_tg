"""平台后台（P5）：总览、代理管理、会员管理、额度台账与到期看板。

平台账号是账号树的根，平台后台看的是**全量**账号与租户，不按调用者的子树裁剪。

三条口径（与设计文档一致）：

1. 平台开的号不占任何额度（``quota_type='none'``）；指定归属代理时从该代理账上扣 1 个；
2. 平台后台的列表**只**含账号与到期信息，绝不含业务数据（TG 账号、机器人、线路、线索）；
3. "今日新开 / 今日续期"按审计日志统计，口径与审计页一致（审计中间件记录全部写请求）。

本模块只做读取与组合，写操作一律复用既有链路（``provision_service`` /
``quota_service`` / ``agent_service``），避免出现第二套账。
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.expiry import local_today, resolve_timezone
from app.db.base import as_utc
from app.db.models import (
    ACCOUNT_TYPE_AGENT,
    ACCOUNT_TYPE_MEMBER,
    TENANT_KIND_MEMBER,
    TENANT_STATUS_EXPIRED,
    TENANT_STATUS_SUSPENDED,
    AgentQuota,
    AuditLog,
    QuotaLedger,
    Tenant,
    TenantLimit,
    TenantModule,
    User,
)
from app.services import (
    agent_service,
    quota_service,
    reminder_service,
    tenant_module_service,
    tenant_status_service,
)

# 到期看板"已过期"分组的回溯窗口（天）
EXPIRED_WINDOW_DAYS = 30
# 总览默认的"即将到期"窗口（天）
EXPIRING_DAYS = 7
# 开号与续期的审计路径：今日新开 / 今日续期的统计口径
OPEN_AUDIT_PATHS = (
    "/api/agent/members",
    "/api/agent/trials",
    "/api/agent/agents",
    "/api/platform/members",
)
RENEW_AUDIT_SUFFIX = "/renew"
# 排序用的"无穷远"时间（没有到期日的排在最后）
_FAR_FUTURE = datetime(9999, 1, 1, tzinfo=UTC)


def _moment(now: datetime | None = None) -> datetime:
    return as_utc(now) or datetime.now(UTC)


def _day_start(now: datetime, tz_name: str | None = None) -> datetime:
    """本地"今天 00:00:00"对应的 UTC 时刻。"""
    tz = resolve_timezone(tz_name)
    local = now.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


async def _all_users(session: AsyncSession) -> list[User]:
    return list(await session.scalars(select(User).order_by(User.id)))


async def _users_by_id(session: AsyncSession, ids: list[int]) -> dict[int, User]:
    if not ids:
        return {}
    rows = await session.scalars(select(User).where(User.id.in_(ids)))
    return {row.id: row for row in rows}


async def _member_tenants(session: AsyncSession) -> list[Tenant]:
    return list(
        await session.scalars(
            select(Tenant).where(Tenant.kind == TENANT_KIND_MEMBER).order_by(Tenant.id)
        )
    )


async def _quotas_by_user(
    session: AsyncSession,
    ids: list[int],
) -> dict[int, AgentQuota]:
    if not ids:
        return {}
    rows = await session.scalars(select(AgentQuota).where(AgentQuota.user_id.in_(ids)))
    return {row.user_id: row for row in rows}


async def _modules_by_tenant(
    session: AsyncSession,
    tenant_ids: list[int],
) -> dict[int, list[str]]:
    if not tenant_ids:
        return {}
    rows = await session.scalars(
        select(TenantModule).where(
            TenantModule.tenant_id.in_(tenant_ids),
            TenantModule.enabled.is_(True),
        )
    )
    result: dict[int, list[str]] = {}
    for row in rows:
        result.setdefault(row.tenant_id, []).append(row.module)
    for modules in result.values():
        modules.sort()
    return result


async def _limits_by_tenant(
    session: AsyncSession,
    tenant_ids: list[int],
) -> dict[int, dict[str, Any]]:
    if not tenant_ids:
        return {}
    rows = await session.scalars(select(TenantLimit).where(TenantLimit.tenant_id.in_(tenant_ids)))
    return {
        row.tenant_id: {
            "max_routes": row.max_routes,
            "max_tg_accounts": row.max_tg_accounts,
            "max_sources": row.max_sources,
            "allow_export": bool(row.allow_export),
        }
        for row in rows
    }


def held_counts(tenants: list[Tenant], *, owner_id: int) -> dict[str, int]:
    """某个代理"已开出去还没到期"占用的额度数。"""
    counts = {"member": 0, "trial": 0}
    for tenant in tenants:
        if tenant.owner_agent_id != owner_id or not tenant.quota_held:
            continue
        if tenant.quota_type in counts:
            counts[tenant.quota_type] += 1
    return counts


def member_row(
    user: User,
    tenant: Tenant,
    *,
    agent_username: str | None,
    modules: list[str],
    limits: dict[str, Any],
    now: datetime,
    tz_name: str | None = None,
) -> dict[str, Any]:
    """会员列表的一行：账号 + 租户 + 功能包 + 到期，不含任何业务数据。"""
    state = tenant_status_service.evaluate(tenant, now=now, tz_name=tz_name)
    return {
        "user_id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "enabled": bool(user.enabled),
        "agent_user_id": tenant.owner_agent_id,
        "agent_username": agent_username,
        "tenant_id": tenant.id,
        "tenant_name": tenant.name,
        "status": state.status,
        "status_label": state.label,
        "expires_at": state.expires_at,
        "days_left": state.days_left,
        "modules": modules,
        "module_labels": tenant_module_service.module_labels(modules),
        "limits": limits,
        "quota_type": tenant.quota_type,
        "quota_held": bool(tenant.quota_held),
        "runtime_enabled": state.runtime_enabled,
        "created_at": as_utc(user.created_at),
    }


async def _today_stats(
    session: AsyncSession,
    *,
    moment: datetime,
    tz_name: str | None = None,
) -> dict[str, int]:
    """今日新开 / 今日续期：数审计日志里的成功写请求。"""
    start = _day_start(moment, tz_name)
    rows = await session.execute(
        select(AuditLog.path).where(
            AuditLog.created_at >= start,
            AuditLog.method == "POST",
            AuditLog.status_code < 300,
        )
    )
    opened = 0
    renewed = 0
    for (path,) in rows.all():
        value = str(path or "")
        if value in OPEN_AUDIT_PATHS:
            opened += 1
        elif value.endswith(RENEW_AUDIT_SUFFIX):
            renewed += 1
    return {"opened": opened, "renewed": renewed}


async def _attach_reminders(
    session: AsyncSession,
    items: list[dict[str, Any]],
) -> None:
    """就地补上 ``reminded_stages``（P4-05 的"已提醒"标记）。"""
    if not items:
        return
    stages = await reminder_service.stages_by_tenant(session, [item["tenant_id"] for item in items])
    for item in items:
        item["reminded_stages"] = stages.get(item["tenant_id"], [])


async def overview(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    tz_name: str | None = None,
    expiring_days: int = EXPIRING_DAYS,
    limit: int = 20,
) -> dict[str, Any]:
    """平台总览（P5-01）：账号分布、今日动作、即将到期、额度预警。"""
    moment = _moment(now)
    window = max(1, int(expiring_days))
    users = await _all_users(session)
    by_id = {user.id: user for user in users}
    agents = [user for user in users if user.account_type == ACCOUNT_TYPE_AGENT]
    members = [user for user in users if user.account_type == ACCOUNT_TYPE_MEMBER]
    tenants = await _member_tenants(session)
    modules = await _modules_by_tenant(session, [tenant.id for tenant in tenants])
    limits = await _limits_by_tenant(session, [tenant.id for tenant in tenants])

    active = expiring = expired = suspended = 0
    expiring_items: list[dict[str, Any]] = []
    for tenant in tenants:
        state = tenant_status_service.evaluate(tenant, now=moment, tz_name=tz_name)
        if state.status == TENANT_STATUS_SUSPENDED:
            suspended += 1
            continue
        if state.status == TENANT_STATUS_EXPIRED:
            expired += 1
            continue
        active += 1
        left = state.days_left
        if left is None or left > window:
            continue
        expiring += 1
        owner = by_id.get(tenant.owner_user_id) if tenant.owner_user_id else None
        if owner is None:
            continue
        agent = by_id.get(tenant.owner_agent_id) if tenant.owner_agent_id else None
        expiring_items.append(
            member_row(
                owner,
                tenant,
                agent_username=None if agent is None else agent.username,
                modules=modules.get(tenant.id, []),
                limits=limits.get(tenant.id, {}),
                now=moment,
                tz_name=tz_name,
            )
        )
    expiring_items.sort(key=lambda item: item["expires_at"] or _FAR_FUTURE)
    await _attach_reminders(session, expiring_items)

    quotas = await _quotas_by_user(session, [agent.id for agent in agents])
    alerts: list[dict[str, Any]] = []
    for agent in agents:
        held = held_counts(tenants, owner_id=agent.id)
        if held["member"] <= 0:
            continue
        balance = quota_service.balances(quotas.get(agent.id))["member"]
        if balance > 0:
            continue
        alerts.append(
            {
                "user_id": agent.id,
                "username": agent.username,
                "display_name": agent.display_name,
                "balance": balance,
                "held": held["member"],
            }
        )

    return {
        "generated_at": moment,
        "expiring_days": window,
        "counts": {
            "agents": len(agents),
            "agents_enabled": sum(1 for user in agents if user.enabled),
            "members": len(members),
            "members_enabled": sum(1 for user in members if user.enabled),
            "active": active,
            "expiring": expiring,
            "expired": expired,
            "suspended": suspended,
        },
        "today": await _today_stats(session, moment=moment, tz_name=tz_name),
        "expiring": expiring_items[:limit],
        "quota_alerts": alerts,
    }


async def list_agents(
    session: AsyncSession,
    *,
    keyword: str | None = None,
    enabled: bool | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """代理管理列表（P5-02）：账号 + 上下级数 + 额度，绝不含业务数据。"""
    moment = _moment(now)
    users = await _all_users(session)
    by_id = {user.id: user for user in users}
    children: dict[int, list[User]] = {}
    for user in users:
        if user.parent_user_id is not None:
            children.setdefault(user.parent_user_id, []).append(user)

    def subtree_counts(root_id: int) -> tuple[int, int]:
        agents_count = 0
        members_count = 0
        stack = list(children.get(root_id, []))
        while stack:
            node = stack.pop()
            if node.account_type == ACCOUNT_TYPE_AGENT:
                agents_count += 1
            elif node.account_type == ACCOUNT_TYPE_MEMBER:
                members_count += 1
            stack.extend(children.get(node.id, []))
        return agents_count, members_count

    def depth_of(user: User) -> int:
        depth = 1
        cursor = user
        seen = {user.id}
        while cursor.parent_user_id is not None:
            parent = by_id.get(cursor.parent_user_id)
            if parent is None or parent.id in seen:
                break
            seen.add(parent.id)
            depth += 1
            cursor = parent
        return depth

    agent_users = [user for user in users if user.account_type == ACCOUNT_TYPE_AGENT]
    tenants = await _member_tenants(session)
    quotas = await _quotas_by_user(session, [user.id for user in agent_users])
    needle = (keyword or "").strip().lower()

    items: list[dict[str, Any]] = []
    for agent in agent_users:
        if (
            needle
            and needle not in (agent.username or "").lower()
            and needle not in (agent.display_name or "").lower()
        ):
            continue
        if enabled is not None and bool(agent.enabled) != bool(enabled):
            continue
        parent = by_id.get(agent.parent_user_id) if agent.parent_user_id else None
        direct_children = children.get(agent.id, [])
        subtree_agents, subtree_members = subtree_counts(agent.id)
        items.append(
            {
                "user_id": agent.id,
                "username": agent.username,
                "display_name": agent.display_name,
                "enabled": bool(agent.enabled),
                "depth": depth_of(agent),
                "parent_user_id": agent.parent_user_id,
                "parent_username": None if parent is None else parent.username,
                "direct_agents": sum(
                    1 for child in direct_children if child.account_type == ACCOUNT_TYPE_AGENT
                ),
                "direct_members": sum(
                    1 for child in direct_children if child.account_type == ACCOUNT_TYPE_MEMBER
                ),
                "subtree_agents": subtree_agents,
                "subtree_members": subtree_members,
                "quota": quota_service.serialize_quota(quotas.get(agent.id)),
                "held": held_counts(tenants, owner_id=agent.id),
                "created_at": as_utc(agent.created_at),
            }
        )
    items.sort(key=lambda item: (item["depth"], item["user_id"]))
    return {"items": items, "total": len(items), "generated_at": moment}


async def list_members(
    session: AsyncSession,
    *,
    agent_user_id: int | None = None,
    status: str | None = None,
    module: str | None = None,
    quota_type: str | None = None,
    keyword: str | None = None,
    limit: int = 50,
    offset: int = 0,
    now: datetime | None = None,
    tz_name: str | None = None,
) -> dict[str, Any]:
    """会员管理列表（P5-03）：支持按代理 / 状态 / 功能包 / 额度类型筛选。"""
    moment = _moment(now)
    users = await _all_users(session)
    by_id = {user.id: user for user in users}
    members = [user for user in users if user.account_type == ACCOUNT_TYPE_MEMBER]
    tenants = {
        tenant.owner_user_id: tenant
        for tenant in await _member_tenants(session)
        if tenant.owner_user_id is not None
    }
    tenant_ids = [tenant.id for tenant in tenants.values()]
    modules = await _modules_by_tenant(session, tenant_ids)
    limits = await _limits_by_tenant(session, tenant_ids)
    needle = (keyword or "").strip().lower()

    items: list[dict[str, Any]] = []
    for user in members:
        tenant = tenants.get(user.id)
        if tenant is None:
            continue
        if agent_user_id is not None and tenant.owner_agent_id != int(agent_user_id):
            continue
        state = tenant_status_service.evaluate(tenant, now=moment, tz_name=tz_name)
        if status and state.status != status:
            continue
        if quota_type and tenant.quota_type != quota_type:
            continue
        row_modules = modules.get(tenant.id, [])
        if module and module not in row_modules:
            continue
        if (
            needle
            and needle not in (user.username or "").lower()
            and needle not in (user.display_name or "").lower()
            and needle not in (tenant.name or "").lower()
        ):
            continue
        agent = by_id.get(tenant.owner_agent_id) if tenant.owner_agent_id else None
        items.append(
            member_row(
                user,
                tenant,
                agent_username=None if agent is None else agent.username,
                modules=row_modules,
                limits=limits.get(tenant.id, {}),
                now=moment,
                tz_name=tz_name,
            )
        )
    items.sort(key=lambda item: (item["expires_at"] or _FAR_FUTURE, item["user_id"]))
    total = len(items)
    return {
        "items": items[offset : offset + limit],
        "total": total,
        "limit": limit,
        "offset": offset,
        "generated_at": moment,
    }


async def expiry_board(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    tz_name: str | None = None,
    expired_window_days: int = EXPIRED_WINDOW_DAYS,
    limit: int = 200,
) -> dict[str, Any]:
    """到期看板（P5-05）：今日到期 / 3 天内 / 7 天内 / 已过期（最近 30 天）。"""
    moment = _moment(now)
    tz = resolve_timezone(tz_name)
    today = local_today(tz_name=tz_name, now=moment)
    horizon = today - timedelta(days=max(1, int(expired_window_days)))

    users = await _all_users(session)
    by_id = {user.id: user for user in users}
    tenants = await _member_tenants(session)
    modules = await _modules_by_tenant(session, [tenant.id for tenant in tenants])
    limits = await _limits_by_tenant(session, [tenant.id for tenant in tenants])

    buckets: dict[str, list[dict[str, Any]]] = {
        "today": [],
        "days3": [],
        "days7": [],
        "expired": [],
    }
    for tenant in tenants:
        expires = as_utc(tenant.expires_at)
        if expires is None or tenant.owner_user_id is None:
            continue
        owner = by_id.get(tenant.owner_user_id)
        if owner is None:
            continue
        local_day = expires.astimezone(tz).date()
        left = (local_day - today).days
        if left < 0:
            if local_day < horizon:
                continue
            key = "expired"
        elif left == 0:
            key = "today"
        elif left <= 3:
            key = "days3"
        elif left <= 7:
            key = "days7"
        else:
            continue
        agent = by_id.get(tenant.owner_agent_id) if tenant.owner_agent_id else None
        buckets[key].append(
            member_row(
                owner,
                tenant,
                agent_username=None if agent is None else agent.username,
                modules=modules.get(tenant.id, []),
                limits=limits.get(tenant.id, {}),
                now=moment,
                tz_name=tz_name,
            )
        )

    labels = {
        "today": "今日到期",
        "days3": "3 天内到期",
        "days7": "7 天内到期",
        "expired": f"已过期（最近 {int(expired_window_days)} 天）",
    }
    payload: dict[str, Any] = {}
    counts: dict[str, int] = {}
    for key, items in buckets.items():
        items.sort(key=lambda item: (item["expires_at"] or _FAR_FUTURE, item["user_id"]))
        await _attach_reminders(session, items)
        counts[key] = len(items)
        payload[key] = {
            "key": key,
            "label": labels[key],
            "count": len(items),
            "items": items[:limit],
        }
    return {
        "generated_at": moment,
        "today": today,
        "counts": counts,
        "buckets": payload,
    }


async def list_ledger(
    session: AsyncSession,
    *,
    agent_user_id: int | None = None,
    quota_type: str | None = None,
    action: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """额度台账（P5-04）：全部划拨与消耗记录，可按代理 / 类型 / 动作 / 时间筛选。"""
    conditions = []
    if agent_user_id is not None:
        ids = await agent_service.subtree_ids(session, int(agent_user_id))
        conditions.append(QuotaLedger.subject_user_id.in_(ids or [-1]))
    if quota_type:
        conditions.append(QuotaLedger.quota_type == quota_type)
    if action:
        conditions.append(QuotaLedger.action == action)
    if start is not None:
        conditions.append(QuotaLedger.created_at >= as_utc(start))
    if end is not None:
        conditions.append(QuotaLedger.created_at <= as_utc(end))

    statement = select(QuotaLedger)
    count_statement = select(func.count()).select_from(QuotaLedger)
    for condition in conditions:
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)

    rows = list(
        await session.scalars(statement.order_by(QuotaLedger.id.desc()).limit(limit).offset(offset))
    )
    total = int(await session.scalar(count_statement) or 0)
    return {
        "items": [quota_service.serialize_ledger(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def ledger_csv(items: list[dict[str, Any]]) -> str:
    """台账导出（P5-04）：带 BOM，Excel 打开不乱码。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["时间", "操作者", "对象", "额度类型", "变动量", "变动后余额", "动作", "备注"])
    for row in items:
        writer.writerow(
            [
                row.get("created_at") or "",
                row.get("actor_username") or "",
                row.get("subject_username") or "",
                row.get("quota_label") or row.get("quota_type") or "",
                row.get("change"),
                row.get("balance_after"),
                row.get("action_label") or row.get("action") or "",
                row.get("note") or "",
            ]
        )
    return "\ufeff" + buffer.getvalue()


async def agent_tree(
    session: AsyncSession,
    *,
    user_id: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """某个代理的整棵子树（只读）：直属可操作、隔层只读。"""
    user = await session.get(User, int(user_id))
    if user is None:
        raise NotFoundError("账号不存在")
    items = await agent_service.list_subordinates(session, actor=user, now=_moment(now))
    return {
        "account": {
            "user_id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "account_type": user.account_type,
            "enabled": bool(user.enabled),
            "parent_user_id": user.parent_user_id,
        },
        "items": items,
        "total": len(items),
    }
