"""租户运行开关与到期强停（P4-03）。

分两层实现"到点就停"：

1. **投递路径实时过滤**（``is_runtime_allowed``）：真正发消息之前再判一次，
   不依赖心跳周期、不依赖任何重启动作。为了让"到期那一秒"生效，这里缓存的只有
   ``runtime_enabled`` 与 ``expires_at`` 两个原始值，**过期比较仍然每次现算**；
2. **心跳巡检**（``sweep_once``）：把状态落到 ``tenants.expired_at`` /
   ``runtime_enabled`` / ``runtime_stop_reason`` 上，释放到期额度、取消在途任务。

缓存是**进程内**的，TTL 30 秒：

- 命中且放行 → 30 秒内不查库（活跃租户的热路径）；
- 命中但被挡 → 5 秒后就回源（停用 / 过期是少数租户，回源代价可以忽略），
  这样会员点「启动」后最多 5 秒生效。

红线：租户状态判定必须**每次投递都算**，不能像线路配置那样挂在启动注册那一步。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationFailedError
from app.core.expiry import is_expired
from app.db.base import as_utc, utc_now
from app.db.models import (
    STOP_REASON_EXPIRED,
    STOP_REASON_MANUAL,
    STOP_REASON_SUSPENDED,
    TENANT_STATUS_ACTIVE,
    Route,
    Tenant,
)
from app.services import delivery_service, provision_service, tenant_status_service

# 放行结果缓存 30 秒；被挡结果 5 秒后就回源（见模块 docstring）
CACHE_TTL_SECONDS = 30.0
NEGATIVE_TTL_SECONDS = 5.0
# 心跳巡检周期（API 进程后台任务与运行时心跳都用它）
SWEEP_INTERVAL_SECONDS = 60.0

# {tenant_id: (runtime_enabled, expires_at, 载入时刻 monotonic)}
_GATE_CACHE: dict[int, tuple[bool, datetime | None, float]] = {}


def _monotonic() -> float:
    return time.monotonic()


def invalidate(tenant_id: int) -> None:
    """丢掉某个租户的缓存（启停、续期、解停之后调用）。"""
    _GATE_CACHE.pop(int(tenant_id), None)


def clear_cache() -> None:
    """清空全部缓存（巡检与测试用）。"""
    _GATE_CACHE.clear()


def cache_size() -> int:
    """当前缓存条目数（测试用）。"""
    return len(_GATE_CACHE)


async def _load_gate(session: AsyncSession, tenant_id: int) -> tuple[bool, datetime | None]:
    """回源读租户的原始开关与到期时间。"""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return False, None
    return bool(tenant.runtime_enabled), as_utc(tenant.expires_at)


async def tenant_gate(
    session: AsyncSession,
    tenant_id: int,
    *,
    now: datetime | None = None,
) -> tuple[bool, datetime | None]:
    """取（走缓存的）运行开关与到期时间，供投递 / 入队路径判断。"""
    key = int(tenant_id)
    stamp = _monotonic()
    cached = _GATE_CACHE.get(key)
    if cached is not None:
        enabled, expires, loaded_at = cached
        allowed = enabled and not is_expired(expires, now=now)
        if stamp - loaded_at < (CACHE_TTL_SECONDS if allowed else NEGATIVE_TTL_SECONDS):
            return enabled, expires
    enabled, expires = await _load_gate(session, tenant_id)
    _GATE_CACHE[key] = (enabled, expires, stamp)
    return enabled, expires


async def is_runtime_allowed(
    session: AsyncSession,
    tenant_id: int | None,
    *,
    now: datetime | None = None,
) -> bool:
    """该租户现在能不能干活：运行开关打开 **且** 未过期、未停用。

    租户不存在（数据异常）时返回 ``False``——宁可少发，不可错发。
    """
    if tenant_id is None:
        return True
    enabled, expires = await tenant_gate(session, tenant_id, now=now)
    return bool(enabled) and not is_expired(expires, now=now)


async def start_tenant_runtime(
    session: AsyncSession,
    tenant: Tenant,
    *,
    actor_username: str | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """会员"手动启动"：打开运行总开关，清掉停止原因。

    过期 / 停用的租户不能启动（要先由上级续期或解停）。
    """
    state = tenant_status_service.effective_status(tenant, now=now)
    if state != TENANT_STATUS_ACTIVE:
        raise ValidationFailedError(tenant_status_service.BLOCKED_MESSAGE)
    tenant.runtime_enabled = True
    tenant.runtime_stop_reason = None
    await session.flush()
    if commit:
        await session.commit()
        await session.refresh(tenant)
    invalidate(tenant.id)
    return {
        "tenant_id": tenant.id,
        "runtime_enabled": True,
        "runtime_stop_reason": None,
        "actor": actor_username,
    }


async def stop_tenant_runtime(
    session: AsyncSession,
    tenant: Tenant,
    *,
    reason: str = STOP_REASON_MANUAL,
    cancel_jobs: bool = True,
    now: datetime | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """停止租户功能：关总开关、记原因，并把排队中的任务取消掉。

    之所以连排队任务一起取消：投递循环按任务 ID 顺序取，留着一个"永不投递"的
    队头会把后面所有租户堵死（行头阻塞）。
    """
    tenant.runtime_enabled = False
    tenant.runtime_stop_reason = reason
    await session.flush()
    cancelled = 0
    if cancel_jobs:
        cancelled = await delivery_service.cancel_tenant_jobs(
            session,
            tenant_id=tenant.id,
            reason=_stop_job_reason(reason),
            commit=False,
        )
    if commit:
        await session.commit()
        await session.refresh(tenant)
    invalidate(tenant.id)
    return {
        "tenant_id": tenant.id,
        "runtime_enabled": False,
        "runtime_stop_reason": reason,
        "cancelled_jobs": cancelled,
    }


async def enable_all_routes(session: AsyncSession, tenant_id: int) -> int:
    """把某租户的线路全部恢复为启用（"一键启动全部线路"）。

    只开不关：已经是启用的行不动，避免无谓地刷新 ``updated_at``。
    """
    result = await session.execute(
        update(Route)
        .where(Route.tenant_id == tenant_id, Route.enabled.is_(False))
        .values(enabled=True)
    )
    await session.commit()
    return int(result.rowcount or 0)


def _stop_job_reason(reason: str) -> str:
    return {
        STOP_REASON_EXPIRED: "账号已过期，投递任务已取消",
        STOP_REASON_SUSPENDED: "账号已停用，投递任务已取消",
    }.get(reason, "账号已停止运行，投递任务已取消")


async def sweep_once(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    tz_name: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """心跳巡检：逐租户判状态 → 强停 → 释放额度 → 取消在途任务 → 清缓存。

    幂等：重复执行不会重复释放额度（P3 靠 ``quota_held``），也不会重复取消任务
    （`pending`/`retrying` 已经没有了）。所以这个方法可以每隔一分钟无脑跑。
    """
    moment = as_utc(now) or utc_now()
    summary: dict[str, Any] = {
        "checked": 0,
        "expired": [],
        "suspended": [],
        "released": [],
        "cancelled_jobs": 0,
        "at": moment,
    }

    tenants = list(await session.scalars(select(Tenant)))
    for tenant in tenants:
        summary["checked"] += 1
        status = tenant_status_service.effective_status(tenant, now=moment)
        first_time = False
        if status == tenant_status_service.TENANT_STATUS_EXPIRED:
            if tenant.expired_at is None:
                tenant.expired_at = moment
                first_time = True
            if tenant.runtime_enabled or tenant.runtime_stop_reason != STOP_REASON_EXPIRED:
                tenant.runtime_enabled = False
                tenant.runtime_stop_reason = STOP_REASON_EXPIRED
                first_time = True
            if await provision_service.expire_release(
                session,
                tenant=tenant,
                actor_username="system",
                note="账号到期，自动释放额度",
                commit=False,
            ):
                summary["released"].append(tenant.id)
            if first_time:
                summary["expired"].append(tenant.id)
        elif status == tenant_status_service.TENANT_STATUS_SUSPENDED:
            # 停用**不释放额度**：额度还握在代理手上，解停后继续用
            if tenant.runtime_enabled or tenant.runtime_stop_reason != STOP_REASON_SUSPENDED:
                tenant.runtime_enabled = False
                tenant.runtime_stop_reason = STOP_REASON_SUSPENDED
                first_time = True
            if first_time:
                summary["suspended"].append(tenant.id)
        else:
            continue

        # 只有"这一轮刚判定出来"的才需要清队列：之后队列本来就空了
        if first_time:
            summary["cancelled_jobs"] += await delivery_service.cancel_tenant_jobs(
                session,
                tenant_id=tenant.id,
                reason=_stop_job_reason(tenant.runtime_stop_reason or STOP_REASON_EXPIRED),
                commit=False,
            )
        invalidate(tenant.id)

    await session.flush()
    if commit:
        await session.commit()
    _ = tz_name  # 预留：将来按租户时区判定时使用
    return summary
