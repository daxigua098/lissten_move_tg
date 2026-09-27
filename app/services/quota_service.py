"""额度服务：划拨、回收、开号消耗、到期释放、手工调账与流水（P3-02/P3-03）。

四条规矩：

1. **余额只能通过本模块变动**——所有变动都是"先校验、再原子扣减、后写流水"；
2. **扣减用条件更新**（``WHERE 余额 >= N``），影响行数为 0 即额度不足，
   这是本项目唯一需要防"并发超发"的地方；
3. **一次划拨写两行流水**（上级 −N、下级 +N），共享同一个 ``transfer_id``；
4. **默认提交事务**；开号链路要在同一事务里写多张表，传 ``commit=False``，
   由调用方统一提交，任一步失败整体回滚。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    InsufficientQuotaError,
    NotDirectSubordinateError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.db.models import (
    ACCOUNT_TYPE_AGENT,
    ACTION_ALLOCATE_IN,
    ACTION_ALLOCATE_OUT,
    ACTION_EXPIRE_RELEASE,
    ACTION_LABELS,
    ACTION_MANUAL_ADJUST,
    ACTION_RECLAIM_IN,
    ACTION_RECLAIM_OUT,
    ACTION_RENEW_CONSUME,
    QUOTA_FIELD,
    QUOTA_LABELS,
    QUOTA_TYPES,
    AgentQuota,
    QuotaLedger,
    User,
)


def validate_quota_type(quota_type: str) -> str:
    """校验额度类型。"""
    value = (quota_type or "").strip()
    if value not in QUOTA_TYPES:
        raise ValidationFailedError(f"额度类型必须是 {'/'.join(QUOTA_TYPES)} 之一")
    return value


def validate_count(count: int) -> int:
    """校验数量必须为正整数。"""
    try:
        value = int(count)
    except (TypeError, ValueError) as exc:
        raise ValidationFailedError("额度数量必须是整数") from exc
    if value <= 0:
        raise ValidationFailedError("额度数量必须大于 0")
    return value


def quota_label(quota_type: str) -> str:
    """额度类型显示名。"""
    return QUOTA_LABELS.get(quota_type, quota_type)


def action_label(action: str) -> str:
    """流水动作显示名。"""
    return ACTION_LABELS.get(action, action)


async def get_quota(session: AsyncSession, user_id: int) -> AgentQuota | None:
    """取代理额度行；没有则返回 None（= 不受额度限制）。"""
    return await session.scalar(select(AgentQuota).where(AgentQuota.user_id == user_id))


async def get_or_create_quota(
    session: AsyncSession,
    user_id: int,
    *,
    flush: bool = True,
) -> AgentQuota:
    """取代理额度行，没有就建一行零余额（开下级代理时也会用到）。"""
    row = await get_quota(session, user_id)
    if row is not None:
        return row
    user = await session.get(User, user_id)
    if user is None:
        raise ValidationFailedError("账号不存在")
    row = AgentQuota(user_id=user_id, member_quota=0, agent_quota=0, trial_quota=0)
    session.add(row)
    if flush:
        await session.flush()
    return row


def balances(row: AgentQuota | None) -> dict[str, int]:
    """把余额行转成 ``{额度类型: 余额}``；``None`` 返回三个 0。"""
    if row is None:
        return {quota: 0 for quota in QUOTA_TYPES}
    return {quota: int(getattr(row, QUOTA_FIELD[quota])) for quota in QUOTA_TYPES}


async def balance_of(session: AsyncSession, user_id: int, quota_type: str) -> int:
    """读某个代理某类额度的当前余额（绕过会话缓存，直接查库）。"""
    quota = validate_quota_type(quota_type)
    field = QUOTA_FIELD[quota]
    value = await session.scalar(
        select(getattr(AgentQuota, field)).where(AgentQuota.user_id == user_id)
    )
    return int(value or 0)


async def _change(
    session: AsyncSession,
    *,
    user_id: int,
    quota_type: str,
    delta: int,
) -> int:
    """原子增减余额并返回变动后余额；不足时抛 :class:`InsufficientQuotaError`。"""
    quota = validate_quota_type(quota_type)
    if delta == 0:
        raise ValidationFailedError("额度变动量不能为 0")
    await get_or_create_quota(session, user_id)

    field = QUOTA_FIELD[quota]
    column = getattr(AgentQuota, field)
    statement = update(AgentQuota.__table__).where(AgentQuota.__table__.c.user_id == user_id)
    if delta < 0:
        # 条件更新：额度不够就一行都改不到，靠 rowcount 判断，避免并发超发
        statement = statement.where(column + delta >= 0)
    statement = statement.values({field: column + delta})

    result = await session.execute(statement)
    if result.rowcount != 1:
        raise InsufficientQuotaError(
            f"{quota_label(quota)}不足，请先向上级申请划拨",
            extra={"quota_type": quota},
        )
    return await balance_of(session, user_id, quota)


async def _write_ledger(
    session: AsyncSession,
    *,
    subject: User,
    quota_type: str,
    action: str,
    change: int,
    balance_after: int,
    actor_username: str,
    actor_user_id: int | None = None,
    transfer_id: str | None = None,
    related_tenant_id: int | None = None,
    related_user_id: int | None = None,
    note: str | None = None,
) -> QuotaLedger:
    """写一行流水（不提交）。"""
    row = QuotaLedger(
        transfer_id=transfer_id,
        subject_user_id=subject.id,
        subject_username=subject.username,
        quota_type=validate_quota_type(quota_type),
        action=action,
        change=int(change),
        balance_after=int(balance_after),
        related_tenant_id=related_tenant_id,
        related_user_id=related_user_id,
        actor_user_id=actor_user_id,
        actor_username=actor_username or "system",
        note=(note or "").strip() or None,
    )
    session.add(row)
    return row


def _assert_direct_subordinate(actor: User, target: User) -> None:
    """划拨/回收只允许发生在直属下级之间。"""
    if target.id == actor.id:
        raise NotDirectSubordinateError("不能给自己划拨额度")
    if target.parent_user_id != actor.id:
        raise NotDirectSubordinateError()


def _assert_agent_actor(actor: User) -> None:
    """只有代理账号有余额可划；平台账号请走手工调账。"""
    if actor.account_type != ACCOUNT_TYPE_AGENT:
        raise PermissionDeniedError("只有代理账号可以在下级之间划拨额度，平台账号请使用调账")


async def allocate(
    session: AsyncSession,
    *,
    actor: User,
    target: User,
    quota_type: str,
    count: int,
    note: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """上级把额度划拨给直属下级（上级减、下级增，总量守恒）。"""
    quota = validate_quota_type(quota_type)
    amount = validate_count(count)
    _assert_agent_actor(actor)
    _assert_direct_subordinate(actor, target)

    transfer_id = uuid.uuid4().hex
    out_balance = await _change(session, user_id=actor.id, quota_type=quota, delta=-amount)
    in_balance = await _change(session, user_id=target.id, quota_type=quota, delta=amount)
    await _write_ledger(
        session,
        subject=actor,
        quota_type=quota,
        action=ACTION_ALLOCATE_OUT,
        change=-amount,
        balance_after=out_balance,
        actor_user_id=actor.id,
        actor_username=actor.username,
        transfer_id=transfer_id,
        related_user_id=target.id,
        note=note,
    )
    await _write_ledger(
        session,
        subject=target,
        quota_type=quota,
        action=ACTION_ALLOCATE_IN,
        change=amount,
        balance_after=in_balance,
        actor_user_id=actor.id,
        actor_username=actor.username,
        transfer_id=transfer_id,
        related_user_id=actor.id,
        note=note,
    )
    if commit:
        await session.commit()
    return {"transfer_id": transfer_id, "quota_type": quota, "count": amount}


async def reclaim(
    session: AsyncSession,
    *,
    actor: User,
    target: User,
    quota_type: str,
    count: int,
    note: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """上级回收直属下级的**未使用**余额（下级减、上级增）。"""
    quota = validate_quota_type(quota_type)
    amount = validate_count(count)
    _assert_agent_actor(actor)
    _assert_direct_subordinate(actor, target)

    transfer_id = uuid.uuid4().hex
    out_balance = await _change(session, user_id=target.id, quota_type=quota, delta=-amount)
    in_balance = await _change(session, user_id=actor.id, quota_type=quota, delta=amount)
    await _write_ledger(
        session,
        subject=target,
        quota_type=quota,
        action=ACTION_RECLAIM_OUT,
        change=-amount,
        balance_after=out_balance,
        actor_user_id=actor.id,
        actor_username=actor.username,
        transfer_id=transfer_id,
        related_user_id=actor.id,
        note=note,
    )
    await _write_ledger(
        session,
        subject=actor,
        quota_type=quota,
        action=ACTION_RECLAIM_IN,
        change=amount,
        balance_after=in_balance,
        actor_user_id=actor.id,
        actor_username=actor.username,
        transfer_id=transfer_id,
        related_user_id=target.id,
        note=note,
    )
    if commit:
        await session.commit()
    return {"transfer_id": transfer_id, "quota_type": quota, "count": amount}


async def consume(
    session: AsyncSession,
    *,
    actor: User,
    subject: User,
    quota_type: str,
    action: str,
    count: int = 1,
    related_tenant_id: int | None = None,
    related_user_id: int | None = None,
    note: str | None = None,
    commit: bool = True,
) -> int:
    """开号消耗额度（单边扣减 + 一行流水），返回扣减后余额。"""
    quota = validate_quota_type(quota_type)
    amount = validate_count(count)
    balance = await _change(session, user_id=subject.id, quota_type=quota, delta=-amount)
    await _write_ledger(
        session,
        subject=subject,
        quota_type=quota,
        action=action,
        change=-amount,
        balance_after=balance,
        actor_user_id=actor.id,
        actor_username=actor.username,
        related_tenant_id=related_tenant_id,
        related_user_id=related_user_id,
        note=note,
    )
    if commit:
        await session.commit()
    return balance


async def release(
    session: AsyncSession,
    *,
    subject: User,
    quota_type: str,
    count: int = 1,
    related_tenant_id: int | None = None,
    actor_username: str = "system",
    actor_user_id: int | None = None,
    note: str | None = None,
    commit: bool = True,
) -> int:
    """到期释放额度（单边增加 + 一行流水），返回释放后余额。"""
    quota = validate_quota_type(quota_type)
    amount = validate_count(count)
    balance = await _change(session, user_id=subject.id, quota_type=quota, delta=amount)
    await _write_ledger(
        session,
        subject=subject,
        quota_type=quota,
        action=ACTION_EXPIRE_RELEASE,
        change=amount,
        balance_after=balance,
        actor_user_id=actor_user_id,
        actor_username=actor_username,
        related_tenant_id=related_tenant_id,
        note=note,
    )
    if commit:
        await session.commit()
    return balance


async def renew_consume(
    session: AsyncSession,
    *,
    actor: User,
    subject: User,
    related_tenant_id: int | None = None,
    note: str | None = None,
    commit: bool = True,
) -> int:
    """续期重新占用 1 个会员额度。"""
    return await consume(
        session,
        actor=actor,
        subject=subject,
        quota_type="member",
        action=ACTION_RENEW_CONSUME,
        count=1,
        related_tenant_id=related_tenant_id,
        note=note,
        commit=commit,
    )


async def manual_adjust(
    session: AsyncSession,
    *,
    actor: User,
    target: User,
    quota_type: str,
    delta: int,
    note: str,
    commit: bool = True,
) -> int:
    """平台手工调账（可正可负），必须填备注；返回调整后余额。"""
    quota = validate_quota_type(quota_type)
    if delta == 0:
        raise ValidationFailedError("调账数量不能为 0")
    if not (note or "").strip():
        raise ValidationFailedError("手工调账必须填写备注")
    balance = await _change(session, user_id=target.id, quota_type=quota, delta=int(delta))
    await _write_ledger(
        session,
        subject=target,
        quota_type=quota,
        action=ACTION_MANUAL_ADJUST,
        change=int(delta),
        balance_after=balance,
        actor_user_id=actor.id,
        actor_username=actor.username,
        related_user_id=target.id,
        note=note,
    )
    if commit:
        await session.commit()
    return balance


async def list_ledger(
    session: AsyncSession,
    *,
    subject_user_id: int | None = None,
    subject_user_ids: list[int] | None = None,
    quota_type: str | None = None,
    action: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[QuotaLedger], int]:
    """分页查询额度流水（新的在前）。"""
    statement = select(QuotaLedger)
    count_statement = select(func.count()).select_from(QuotaLedger)
    if subject_user_id is not None:
        statement = statement.where(QuotaLedger.subject_user_id == subject_user_id)
        count_statement = count_statement.where(QuotaLedger.subject_user_id == subject_user_id)
    if subject_user_ids is not None:
        ids = list(subject_user_ids) or [-1]
        statement = statement.where(QuotaLedger.subject_user_id.in_(ids))
        count_statement = count_statement.where(QuotaLedger.subject_user_id.in_(ids))
    if quota_type:
        statement = statement.where(QuotaLedger.quota_type == quota_type)
        count_statement = count_statement.where(QuotaLedger.quota_type == quota_type)
    if action:
        statement = statement.where(QuotaLedger.action == action)
        count_statement = count_statement.where(QuotaLedger.action == action)

    rows = list(
        await session.scalars(statement.order_by(QuotaLedger.id.desc()).limit(limit).offset(offset))
    )
    total = int(await session.scalar(count_statement) or 0)
    return rows, total


async def reconcile(session: AsyncSession, user_id: int) -> dict[str, Any]:
    """按流水重算余额并与余额表比对（不一致以流水为准）。"""
    row = await get_quota(session, user_id)
    current = balances(row)
    result: dict[str, Any] = {"user_id": user_id, "balances": current, "ledger_sums": {}}
    consistent = True
    for quota in QUOTA_TYPES:
        total = int(
            await session.scalar(
                select(func.coalesce(func.sum(QuotaLedger.change), 0)).where(
                    QuotaLedger.subject_user_id == user_id,
                    QuotaLedger.quota_type == quota,
                )
            )
            or 0
        )
        result["ledger_sums"][quota] = total
        if total != current[quota]:
            consistent = False
    result["consistent"] = consistent
    return result


def serialize_quota(row: AgentQuota | None) -> dict[str, Any]:
    """额度总览的对外结构。"""
    data = balances(row)
    return {
        "member": data["member"],
        "agent": data["agent"],
        "trial": data["trial"],
        "labels": dict(QUOTA_LABELS),
        "updated_at": None if row is None else row.updated_at,
    }


def serialize_ledger(row: QuotaLedger) -> dict[str, Any]:
    """流水的对外结构。"""
    return {
        "id": row.id,
        "transfer_id": row.transfer_id,
        "subject_user_id": row.subject_user_id,
        "subject_username": row.subject_username,
        "quota_type": row.quota_type,
        "quota_label": quota_label(row.quota_type),
        "action": row.action,
        "action_label": action_label(row.action),
        "change": row.change,
        "balance_after": row.balance_after,
        "related_tenant_id": row.related_tenant_id,
        "related_user_id": row.related_user_id,
        "actor_username": row.actor_username,
        "note": row.note,
        "created_at": row.created_at,
    }
