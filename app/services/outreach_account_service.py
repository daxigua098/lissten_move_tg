"""发信息账号运营态：档位、状态与按天额度快照。

只负责读运营态与调整档位 / 状态；真正的排队与发送在后续阶段接入。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationFailedError
from app.core.expiry import local_today
from app.core.paths import ensure_dir
from app.db.base import as_utc, utc_now
from app.db.models import (
    ACCOUNT_ACTIVE,
    ACCOUNT_DISABLED,
    ACCOUNT_PURPOSE_OUTREACH,
    ACCOUNT_STATE_LABELS,
    ACCOUNT_STATES,
    STATE_DISABLED,
    STATE_LIMITED,
    STATE_PAUSED,
    STATE_READY,
    TASK_QUEUED,
    TIER_DEFAULTS,
    TIER_LABELS,
    TIER_NEW,
    TIERS,
    OutreachAccountDaily,
    OutreachAccountState,
    OutreachTask,
    TgAccount,
)


def local_day(moment: datetime | None = None, *, tz_name: str | None = None) -> date:
    """租户本地日期（配置时区，缺 tzdata 时退回东八区）。"""
    return local_today(tz_name=tz_name, now=moment)


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
    tz_name: str | None = None,
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
            OutreachAccountDaily.day == local_day(now, tz_name=tz_name),
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
        "success_rate_7d": state.success_rate_7d if state is not None else None,
        "reply_rate_7d": state.reply_rate_7d if state is not None else None,
        "active_conversation_count": (state.active_conversation_count if state is not None else 0),
        "metrics_at": as_utc(state.metrics_at) if state is not None else None,
        "note": state.note if state is not None else None,
    }


async def get_or_create_daily(
    session: AsyncSession,
    account: TgAccount,
    *,
    day: date | None = None,
    tz_name: str | None = None,
) -> OutreachAccountDaily:
    """取账号当天的计分行；没有就建一条（调用方负责 commit）。"""
    target = day or local_day(tz_name=tz_name)
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
    tz_name: str | None = None,
    first_contact: int = 0,
    follow_up: int = 0,
    failed: int = 0,
    blocked: int = 0,
    limited: int = 0,
) -> OutreachAccountDaily:
    """累加当天的计数（调用方负责 commit）。"""
    row = await get_or_create_daily(session, account, tz_name=tz_name)
    row.first_contact_sent += first_contact
    row.follow_up_sent += follow_up
    row.failed += failed
    row.blocked += blocked
    row.limited_hits += limited
    return row


async def set_participation(
    session: AsyncSession,
    account_ids: list[int],
    *,
    enabled: bool,
    tenant_id: int,
) -> dict[str, Any]:
    """勾选 / 取消勾选"参与冷触达"。

    参与 = 运营态 READY（冷却、配额、档位、熔断等策略照旧生效）；
    取消 = PAUSED（调度器直接跳过）。被限制 / 停用 / 未登录的账号不能被强行打开。
    """
    updated: list[int] = []
    skipped: list[dict[str, Any]] = []
    for account_id in account_ids:
        account = await session.get(TgAccount, account_id)
        if account is None or account.purpose != ACCOUNT_PURPOSE_OUTREACH:
            skipped.append({"account_id": account_id, "reason": "不是发信息账号"})
            continue
        if tenant_id and account.tenant_id != tenant_id:
            skipped.append({"account_id": account_id, "reason": "不属于当前租户"})
            continue
        if account.status != ACCOUNT_ACTIVE:
            skipped.append({"account_id": account_id, "reason": f"{account.name} 还没登录"})
            continue
        state = await get_or_create_state(session, account)
        if state.state in (STATE_LIMITED, STATE_DISABLED):
            skipped.append(
                {"account_id": account_id, "reason": f"{account.name} 被限制或已停用，不能参与"}
            )
            continue
        state.state = STATE_READY if enabled else STATE_PAUSED
        updated.append(account.id)
    await session.commit()
    return {"updated": updated, "skipped": skipped, "enabled": enabled}


async def retire(
    session: AsyncSession,
    account: TgAccount,
    *,
    reason: str = "manual",
    hard: bool = False,
    delete_session: bool = False,
    session_path: Any = None,
) -> dict[str, Any]:
    """退役一个发信息账号：冻结名下会话、回收在途任务，默认只软删。

    ``hard=True`` 才会真正删掉账号行；联系档案、消息流水与审计记录始终保留。
    """
    from app.services import outreach_sender_service

    frozen = await outreach_sender_service.freeze_account_contacts(
        session,
        account,
        reason=reason,
    )
    recycled = await _recycle_tasks(session, account.id)

    state = await session.get(OutreachAccountState, account.id)
    if state is None:
        state = OutreachAccountState(account_id=account.id, tenant_id=account.tenant_id)
        session.add(state)
    state.state = STATE_DISABLED
    state.note = f"已退役：{reason}"

    account.status = ACCOUNT_DISABLED
    account.retired_at = utc_now()
    account.retire_reason = reason
    account.is_default = False

    moved = _trash_session_file(session_path) if delete_session else None
    if hard:
        await session.delete(account)
    await session.commit()
    return {
        "account_id": account.id,
        "name": account.name,
        "frozen_contacts": frozen,
        "recycled_tasks": recycled,
        "session_moved": moved,
        "hard": hard,
    }


async def _recycle_tasks(session: AsyncSession, account_id: int) -> int:
    """把该账号在途的任务放回队列（换号或等人工处理，不能丢）。"""
    from app.db.models import TASK_ASSIGNED, TASK_SENDING

    rows = list(
        await session.scalars(
            select(OutreachTask).where(
                OutreachTask.account_id == account_id,
                OutreachTask.status.in_((TASK_ASSIGNED, TASK_SENDING)),
            )
        )
    )
    for row in rows:
        row.status = TASK_QUEUED
        row.account_id = None
        row.next_retry_at = None
        row.last_error = "原账号已退役，任务重新排队"
    return len(rows)


def _trash_session_file(session_path: Any) -> str | None:
    """把 session 文件挪进 ``data/session_trash``（可恢复），不直接销毁。"""
    if session_path is None:
        return None
    base = Path(session_path)
    parent = base.parent
    if not parent.exists():
        return None
    trash = parent.parent / "session_trash"
    ensure_dir(trash)
    stem = base.stem if base.suffix == ".session" else base.name
    moved: list[str] = []
    for item in sorted(parent.glob(f"{stem}*")):
        if not item.is_file():
            continue
        target = trash / item.name
        item.replace(target)
        moved.append(item.name)
    return ", ".join(moved) or None


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
