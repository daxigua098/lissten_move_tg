"""资源发现的配额与限速计数（F-R16 / 非功能需求·配额）。

搜索、探测、加群、退群、FLOOD_WAIT 全部按「账号 + 天」累加。超限的处理是
**排队而不是报错**——需求书写得很明确，所以这里只负责如实计数，
由调用方决定是排队还是拒绝。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utc_now
from app.db.models import ResourceQuota, TgAccount

COUNTERS = ("searches", "joins", "leaves", "probes", "flood_waits")


def today() -> date:
    """当前的 UTC 日期（配额按 UTC 天滚动，与库里的时间戳一致）。"""
    return utc_now().date()


async def get_quota(
    session: AsyncSession,
    account_id: int | None,
    *,
    day: date | None = None,
) -> ResourceQuota | None:
    """查某个账号某天的配额；没有记录返回 None。"""
    if account_id is None:
        return None
    return await session.scalar(
        select(ResourceQuota).where(
            ResourceQuota.account_id == int(account_id),
            ResourceQuota.day == (day or today()),
        )
    )


async def get_or_create(
    session: AsyncSession,
    account_id: int,
    *,
    day: date | None = None,
) -> ResourceQuota:
    """取当天配额记录，没有就建一条。"""
    target_day = day or today()
    row = await get_quota(session, account_id, day=target_day)
    if row is not None:
        return row
    row = ResourceQuota(account_id=int(account_id), day=target_day)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def bump(
    session: AsyncSession,
    account_id: int | None,
    *,
    day: date | None = None,
    **deltas: int,
) -> ResourceQuota | None:
    """累加计数。``account_id`` 为空时（没有账号的场景）直接跳过。"""
    if account_id is None:
        return None
    row = await get_or_create(session, account_id, day=day)
    for name in COUNTERS:
        delta = int(deltas.get(name, 0) or 0)
        if delta:
            setattr(row, name, int(getattr(row, name) or 0) + delta)
    await session.commit()
    await session.refresh(row)
    return row


async def board(
    session: AsyncSession,
    *,
    day: date | None = None,
) -> list[dict[str, Any]]:
    """按账号展示当天配额使用情况（P-R03 的配额看板）。"""
    target_day = day or today()
    accounts = list(await session.scalars(select(TgAccount).order_by(TgAccount.id)))
    quotas = {
        row.account_id: row
        for row in await session.scalars(
            select(ResourceQuota).where(ResourceQuota.day == target_day)
        )
    }
    items: list[dict[str, Any]] = []
    for account in accounts:
        row = quotas.get(account.id)
        items.append(
            {
                "account_id": account.id,
                "account_name": account.name,
                "account_status": account.status,
                "day": target_day.isoformat(),
                "searches": int(row.searches) if row else 0,
                "joins": int(row.joins) if row else 0,
                "leaves": int(row.leaves) if row else 0,
                "probes": int(row.probes) if row else 0,
                "flood_waits": int(row.flood_waits) if row else 0,
            }
        )
    return items


async def today_total(session: AsyncSession, *, day: date | None = None) -> dict[str, int]:
    """全部账号当天合计（页面顶部看板）。"""
    target_day = day or today()
    totals = dict.fromkeys(COUNTERS, 0)
    for row in await session.scalars(select(ResourceQuota).where(ResourceQuota.day == target_day)):
        for name in COUNTERS:
            totals[name] += int(getattr(row, name) or 0)
    return totals


async def recent_days(
    session: AsyncSession,
    *,
    days: int = 7,
) -> list[dict[str, Any]]:
    """最近若干天的合计（趋势用）。"""
    since = today() - timedelta(days=max(1, days) - 1)
    rows = list(
        await session.scalars(
            select(ResourceQuota).where(ResourceQuota.day >= since).order_by(ResourceQuota.day)
        )
    )
    buckets: dict[date, dict[str, int]] = {}
    for row in rows:
        bucket = buckets.setdefault(row.day, dict.fromkeys(COUNTERS, 0))
        for name in COUNTERS:
            bucket[name] += int(getattr(row, name) or 0)
    return [{"day": key.isoformat(), **value} for key, value in sorted(buckets.items())]
