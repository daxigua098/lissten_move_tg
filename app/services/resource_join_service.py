"""加群队列：半自动 + 限速 + 审计（F-R12 / F-R14）。

加群是"选错要付出代价"的动作（会被风控），所以设计上刻意保守：

- **半自动**：只有人工勾选后才入队，系统只负责按节奏执行；
- **限速**：默认每小时 10 个、每天 50 个（可配），同账号串行、任务之间留间隔；
- **可追溯**：每个动作写一条审计日志（谁、哪个资源、哪个账号、结果）；
- **需要审批不算失败**：标记 `waiting_approval` 并提醒，不占用失败次数。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.errors import ConflictError, ValidationFailedError
from app.core.telegram_client import classify_join_error, join_chat, leave_chat, resolve_entity
from app.core.telegram_client import join_invite as _join_invite
from app.db.base import as_utc, utc_now
from app.db.models import (
    JOIN_ACTION_JOIN,
    JOIN_ACTION_LEAVE,
    JOIN_ACTIONS,
    JOIN_FAILED,
    JOIN_PENDING,
    JOIN_RUNNING,
    JOIN_SUCCESS,
    JOIN_WAITING_APPROVAL,
    RESOURCE_RETIRED,
    STATE_LEFT,
    ResourceJoinTask,
    TgResource,
)
from app.services import audit_service, resource_quota_service, resource_service

# 相邻两个加群动作之间的最小间隔，以及额外的随机抖动（F-R12「随机间隔」）
JOIN_GAP_SECONDS = 60
JOIN_JITTER_SECONDS = 120


@dataclass(frozen=True)
class QueueSlot:
    """算出来的可执行时间。"""

    scheduled_at: datetime
    reason: str = "ok"


async def resolve_account_id(session: AsyncSession, account_id: int | None) -> int | None:
    """没指定账号时用默认执行账号。"""
    if account_id is not None:
        return int(account_id)
    from app.services import tg_account_service

    account = await tg_account_service.get_default_account(session)
    return account.id if account is not None else None


async def count_since(
    session: AsyncSession,
    account_id: int | None,
    *,
    action: str,
    since: datetime,
) -> int:
    """统计某账号在某个时间点之后排入队列的动作数（含已完成）。"""
    if account_id is None:
        return 0
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ResourceJoinTask)
            .where(
                ResourceJoinTask.account_id == int(account_id),
                ResourceJoinTask.action == action,
                ResourceJoinTask.status != JOIN_FAILED,
                ResourceJoinTask.scheduled_at >= since,
            )
        )
        or 0
    )


async def _last_scheduled_at(
    session: AsyncSession,
    account_id: int | None,
    *,
    action: str,
) -> datetime | None:
    if account_id is None:
        return None
    value = await session.scalar(
        select(func.max(ResourceJoinTask.scheduled_at)).where(
            ResourceJoinTask.account_id == int(account_id),
            ResourceJoinTask.action == action,
        )
    )
    return as_utc(value) if value is not None else None


def _day_start(moment: datetime) -> datetime:
    return datetime.combine(moment.date(), time.min, tzinfo=moment.tzinfo)


async def next_slot(
    session: AsyncSession,
    config: AppConfig,
    *,
    account_id: int | None,
    action: str = JOIN_ACTION_JOIN,
    now: datetime | None = None,
    jitter: bool = True,
) -> QueueSlot:
    """按限速规则算出下一个可执行时间。

    超过当日上限就排到第二天（排队而不是报错）；超过小时上限就排到
    "本小时最后一个动作 + 1 小时"之后。
    """
    section = config.resource
    moment = now or utc_now()
    # 退群与加群共用同一套限速（都算"账号在群里进出的动作"，风控口径一致）
    hourly_limit = section.join_hourly_limit
    daily_limit = section.join_daily_limit

    day_count = await count_since(
        session,
        account_id,
        action=action,
        since=_day_start(moment),
    )
    if day_count >= daily_limit:
        tomorrow = _day_start(moment + timedelta(days=1))
        return QueueSlot(tomorrow, reason="daily_limit")

    hour_count = await count_since(
        session,
        account_id,
        action=action,
        since=moment - timedelta(hours=1),
    )
    if hour_count >= hourly_limit:
        last = await _last_scheduled_at(session, account_id, action=action)
        base = last + timedelta(hours=1) if last is not None else moment + timedelta(hours=1)
        return QueueSlot(max(base, moment), reason="hourly_limit")

    last = await _last_scheduled_at(session, account_id, action=action)
    base = moment
    if last is not None and last + timedelta(seconds=JOIN_GAP_SECONDS) > moment:
        base = last + timedelta(seconds=JOIN_GAP_SECONDS)
    if jitter and base == moment:
        base = moment + timedelta(seconds=random.uniform(0, JOIN_JITTER_SECONDS))
    return QueueSlot(base)


async def enqueue(
    session: AsyncSession,
    config: AppConfig,
    resource: TgResource,
    *,
    account_id: int | None = None,
    action: str = JOIN_ACTION_JOIN,
    now: datetime | None = None,
    jitter: bool = False,
) -> ResourceJoinTask:
    """把资源排进加群 / 退群队列。"""
    if action not in JOIN_ACTIONS:
        raise ValidationFailedError(f"动作必须是 {'/'.join(JOIN_ACTIONS)} 之一")
    if resource.is_blacklisted and action == JOIN_ACTION_JOIN:
        raise ConflictError("该资源在黑名单里，不会加入")

    resolved_account = await resolve_account_id(session, account_id)
    existing = await session.scalar(
        select(ResourceJoinTask).where(
            ResourceJoinTask.resource_id == resource.id,
            ResourceJoinTask.action == action,
            ResourceJoinTask.status.in_((JOIN_PENDING, JOIN_RUNNING, JOIN_WAITING_APPROVAL)),
        )
    )
    if existing is not None:
        return existing

    slot = await next_slot(
        session,
        config,
        account_id=resolved_account,
        action=action,
        now=now,
        jitter=jitter,
    )
    task = ResourceJoinTask(
        resource_id=resource.id,
        account_id=resolved_account,
        action=action,
        status=JOIN_PENDING,
        scheduled_at=slot.scheduled_at,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def next_due_task(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> ResourceJoinTask | None:
    """取一条到点该执行的队列任务。"""
    moment = now or utc_now()
    return await session.scalar(
        select(ResourceJoinTask)
        .where(
            ResourceJoinTask.status == JOIN_PENDING,
            or_(
                ResourceJoinTask.scheduled_at.is_(None),
                ResourceJoinTask.scheduled_at <= moment,
            ),
        )
        .order_by(ResourceJoinTask.scheduled_at.asc().nulls_first(), ResourceJoinTask.id.asc())
    )


async def run_task(
    session: AsyncSession,
    config: AppConfig,
    client: Any,
    task: ResourceJoinTask,
    *,
    actor: str = "runtime",
) -> dict[str, Any]:
    """执行一条队列任务。"""
    resource = await session.get(TgResource, task.resource_id)
    if resource is None:
        task.status = JOIN_FAILED
        task.last_error = "资源已删除"
        task.finished_at = utc_now()
        await session.commit()
        return {"status": JOIN_FAILED, "error": task.last_error}

    task.status = JOIN_RUNNING
    task.attempts += 1
    await session.commit()

    try:
        await _perform(client, resource, action=task.action)
    except Exception as exc:  # noqa: BLE001 - 分类后落库，不往上抛
        return await _handle_failure(
            session,
            config,
            task,
            resource,
            exc=exc,
            actor=actor,
        )

    task.status = JOIN_SUCCESS
    task.last_error = None
    task.finished_at = utc_now()
    if task.action == JOIN_ACTION_LEAVE:
        resource.status = RESOURCE_RETIRED
        resource.resource_state = STATE_LEFT
    await session.commit()
    await resource_quota_service.bump(
        session,
        task.account_id,
        joins=1 if task.action == JOIN_ACTION_JOIN else 0,
        leaves=1 if task.action == JOIN_ACTION_LEAVE else 0,
    )
    await _write_audit(session, task, resource, actor=actor, ok=True)
    logger.info(
        "{}成功：{}（账号 {}）",
        "加群" if task.action == JOIN_ACTION_JOIN else "退群",
        resource.title or resource.tg_id,
        task.account_id,
    )
    return {"status": JOIN_SUCCESS, "resource_id": resource.id, "action": task.action}


async def _perform(client: Any, resource: TgResource, *, action: str) -> None:
    """真正去 Telegram 执行加入 / 退出。"""
    if resource.invite_link and not resource.tg_id:
        invite_hash = resource.invite_link.rstrip("/").rsplit("/", 1)[-1].lstrip("+")
        if action == JOIN_ACTION_JOIN:
            await _join_invite(client, invite_hash)
            return
        raise ValidationFailedError("还没有加入这个私密群，无法退出")

    if resource.tg_id is None:
        raise ValidationFailedError("该资源没有可用的数字 ID")
    entity = await resolve_entity(client, int(resource.tg_id))
    if action == JOIN_ACTION_JOIN:
        await join_chat(client, entity)
    else:
        await leave_chat(client, entity)


async def _handle_failure(
    session: AsyncSession,
    config: AppConfig,
    task: ResourceJoinTask,
    resource: TgResource,
    *,
    exc: BaseException,
    actor: str,
) -> dict[str, Any]:
    """按失败分类决定：重试、等审批、还是判定失败。"""
    category, label = classify_join_error(exc)
    task.last_error = f"{label}（{category}）"[:200]

    if category == "already_member":
        # 其实已经在群里了，算成功
        task.status = JOIN_SUCCESS
        task.finished_at = utc_now()
        await session.commit()
        await resource_quota_service.bump(session, task.account_id, joins=1)
        await _write_audit(session, task, resource, actor=actor, ok=True)
        return {"status": JOIN_SUCCESS, "resource_id": resource.id, "note": label}

    if category == "approval_required":
        # 等审批不算失败，也不占用重试次数
        task.status = JOIN_WAITING_APPROVAL
        await session.commit()
        await _write_audit(session, task, resource, actor=actor, ok=False)
        return {"status": JOIN_WAITING_APPROVAL, "resource_id": resource.id, "note": label}

    if category == "restricted":
        await resource_quota_service.bump(session, task.account_id, flood_waits=1)

    section = config.resource
    retryable = category in {"unknown", "restricted"}
    if retryable and task.attempts < section.max_join_attempts:
        task.status = JOIN_PENDING
        task.scheduled_at = utc_now() + timedelta(seconds=section.join_retry_backoff_seconds)
    else:
        task.status = JOIN_FAILED
        task.finished_at = utc_now()
    await session.commit()
    await _write_audit(session, task, resource, actor=actor, ok=False)
    logger.warning(
        "加群未成功：{}（{}）→ {}",
        resource.title or resource.tg_id,
        label,
        task.status,
    )
    return {
        "status": task.status,
        "resource_id": resource.id,
        "error": task.last_error,
        "retry_at": task.scheduled_at.isoformat() if task.scheduled_at else None,
    }


async def _write_audit(
    session: AsyncSession,
    task: ResourceJoinTask,
    resource: TgResource,
    *,
    actor: str,
    ok: bool,
) -> None:
    """每个加群 / 退群动作写一条审计记录。"""
    await audit_service.write_audit_log(
        session,
        username=actor or "runtime",
        method="JOIN" if task.action == JOIN_ACTION_JOIN else "LEAVE",
        path=f"/api/resources/{resource.id}",
        status_code=200 if ok else 409,
    )


async def stats(session: AsyncSession) -> dict[str, int]:
    """队列概览。"""
    counts: dict[str, int] = {}
    for label, status in (
        ("pending", JOIN_PENDING),
        ("running", JOIN_RUNNING),
        ("success", JOIN_SUCCESS),
        ("failed", JOIN_FAILED),
        ("waiting_approval", JOIN_WAITING_APPROVAL),
    ):
        counts[label] = int(
            await session.scalar(
                select(func.count())
                .select_from(ResourceJoinTask)
                .where(ResourceJoinTask.status == status)
            )
            or 0
        )
    counts["total"] = sum(counts.values())
    return counts


def serialize_task(task: ResourceJoinTask, resource: TgResource | None = None) -> dict[str, Any]:
    """队列任务对外结构。"""
    scheduled = as_utc(task.scheduled_at)
    finished = as_utc(task.finished_at)
    return {
        "id": task.id,
        "resource_id": task.resource_id,
        "resource_title": (resource.title if resource else None),
        "resource_name": resource_service.serialize_resource(resource)["name"]
        if resource is not None
        else None,
        "account_id": task.account_id,
        "action": task.action,
        "status": task.status,
        "attempts": task.attempts,
        "last_error": task.last_error,
        "scheduled_at": scheduled.isoformat() if scheduled else None,
        "finished_at": finished.isoformat() if finished else None,
    }
