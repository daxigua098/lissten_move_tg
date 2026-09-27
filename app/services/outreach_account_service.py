"""发信息账号运营态：档位、状态与按天额度快照。

只负责读运营态与调整档位 / 状态；真正的排队与发送在后续阶段接入。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationFailedError
from app.core.expiry import local_today
from app.db.base import as_utc
from app.db.models import (
    ACCOUNT_STATE_LABELS,
    ACCOUNT_STATES,
    TIER_DEFAULTS,
    TIER_LABELS,
    TIER_NEW,
    TIERS,
    OutreachAccountDaily,
    OutreachAccountState,
    TgAccount,
)


def local_day(moment: datetime | None = None) -> date:
    """租户本地日期（配置时区，缺 tzdata 时退回东八区）。"""
    return local_today(now=moment)


def tier_limits(tier: str) -> tuple[int, int]:
    """档位默认的 ``(每日首次私聊上限, 首触冷却秒数)``。"""
    return TIER_DEFAULTS.get(tier, TIER_DEFAULTS[TIER_NEW])


async def get_or_create_state(session: AsyncSession, account: TgAccount) -> OutreachAccountState:
    """取账号运营态；没有就按默认档位建一条。"""
    state = await session.get(OutreachAccountState, account.id)
    if state is None:
        state = OutreachAccountState(account_id=account.id, tenant_id=account.tenant_id)
        session.add(state)
        await session.flush()
    return state


async def snapshot(
    session: AsyncSession,
    account: TgAccount,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """额度与冷却快照（只读，列表展示用）。"""
    state = await session.get(OutreachAccountState, account.id)
    tier = state.tier if state is not None else TIER_NEW
    cap, default_cooldown = tier_limits(tier)
    daily_cap = state.daily_cap if state and state.daily_cap else cap
    cooldown = state.cooldown_seconds if state and state.cooldown_seconds else default_cooldown

    counter = await session.scalar(
        select(OutreachAccountDaily).where(
            OutreachAccountDaily.account_id == account.id,
            OutreachAccountDaily.day == local_day(now),
        )
    )
    today_sent = counter.first_contact_sent if counter is not None else 0

    moment = now or datetime.now(UTC)
    cooldown_until = None
    if state is not None and state.last_cold_at is not None:
        candidate = as_utc(state.last_cold_at) + timedelta(seconds=cooldown)
        if candidate > moment:
            cooldown_until = candidate

    return {
        "tier": tier,
        "tier_label": TIER_LABELS.get(tier, tier),
        "state": state.state if state is not None else "NEW",
        "state_label": ACCOUNT_STATE_LABELS.get(
            state.state if state is not None else "NEW",
            state.state if state is not None else "NEW",
        ),
        "daily_cap": daily_cap,
        "today_sent": today_sent,
        "remaining": max(0, daily_cap - today_sent),
        "cooldown_seconds": cooldown,
        "cooldown_until": cooldown_until,
        "limited_until": as_utc(state.limited_until) if state is not None else None,
        "note": state.note if state is not None else None,
    }


async def get_or_create_daily(
    session: AsyncSession,
    account: TgAccount,
    *,
    day: date | None = None,
) -> OutreachAccountDaily:
    """取账号当天的计分行；没有就建一条（调用方负责 commit）。"""
    target = day or local_day()
    row = await session.scalar(
        select(OutreachAccountDaily).where(
            OutreachAccountDaily.account_id == account.id,
            OutreachAccountDaily.day == target,
        )
    )
    if row is None:
        row = OutreachAccountDaily(account_id=account.id, tenant_id=account.tenant_id, day=target)
        session.add(row)
        await session.flush()
    return row


async def bump_daily(
    session: AsyncSession,
    account: TgAccount,
    *,
    first_contact: int = 0,
    follow_up: int = 0,
    failed: int = 0,
    blocked: int = 0,
    limited: int = 0,
) -> OutreachAccountDaily:
    """累加当天的计数（调用方负责 commit）。"""
    row = await get_or_create_daily(session, account)
    row.first_contact_sent += first_contact
    row.follow_up_sent += follow_up
    row.failed += failed
    row.blocked += blocked
    row.limited_hits += limited
    return row


async def update_state(
    session: AsyncSession,
    account: TgAccount,
    *,
    tier: str | None = None,
    state: str | None = None,
    note: str | None = None,
) -> OutreachAccountState:
    """调整档位 / 运营态（人工操作）。"""
    row = await get_or_create_state(session, account)
    if tier is not None:
        if tier not in TIERS:
            raise ValidationFailedError(f"账号档位必须是 {'/'.join(TIERS)} 之一")
        row.tier = tier
    if state is not None:
        if state not in ACCOUNT_STATES:
            raise ValidationFailedError(f"账号状态必须是 {'/'.join(ACCOUNT_STATES)} 之一")
        row.state = state
    if note is not None:
        row.note = note.strip() or None
    await session.commit()
    await session.refresh(row)
    return row
