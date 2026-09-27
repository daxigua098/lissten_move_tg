"""冷触达任务入队（dry-run）：按闸门把线索变成排队任务，本阶段不发送。"""

from __future__ import annotations

import json
from datetime import datetime
from math import ceil

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outreach_capture import (
    OUTREACH_CONTACTED,
    OUTREACH_REFUSED,
    OUTREACH_REPLIED,
    OUTREACH_WAITING_SENDER_ACCOUNT,
    ROUTE_PHONE,
    ROUTE_USERNAME,
    parse_reachable_routes,
)
from app.db.base import as_utc, utc_now
from app.db.models import (
    ACCOUNT_ACTIVE,
    ACCOUNT_PURPOSE_OUTREACH,
    CONTACT_QUEUED,
    STATE_CAPPED,
    STATE_COOLING,
    STATE_DISABLED,
    STATE_LIMITED,
    STATE_PAUSED,
    TASK_ASSIGNED,
    TASK_FIRST_CONTACT,
    TASK_QUEUED,
    ContactSuppression,
    Lead,
    MemberProfile,
    OutreachAccountState,
    OutreachContact,
    OutreachTask,
    TgAccount,
)
from app.db.tenant_context import scoped_tenant_id
from app.services import outreach_account_service, outreach_settings_service

# 闸门原因：对外展示用中文，内部存常量
BLOCK_KILL_SWITCH = "KILL_SWITCH"
BLOCK_DO_NOT_CONTACT = "DO_NOT_CONTACT"
BLOCK_IDENTITY = "IDENTITY_UNCONFIRMED"
BLOCK_NO_CONTACT = "NO_DIRECT_CONTACT"
BLOCK_ALREADY_CONTACTED = "ALREADY_CONTACTED"
BLOCK_GLOBAL_LOCK = "GLOBAL_LOCK"
BLOCK_OWNER = "CONVERSATION_OWNER"
BLOCK_DUPLICATE = "DUPLICATE"

BLOCK_LABELS: dict[str, str] = {
    BLOCK_KILL_SWITCH: "冷触达已熔断",
    BLOCK_DO_NOT_CONTACT: "在免打扰名单里",
    BLOCK_IDENTITY: "拿不到稳定的 Telegram 用户 ID",
    BLOCK_NO_CONTACT: "缺少可直接寻址方式（用户名 / 手机号 / 正文联系方式）",
    BLOCK_ALREADY_CONTACTED: "这个人已经联系过",
    BLOCK_GLOBAL_LOCK: "在跨账号锁定期内",
    BLOCK_OWNER: "会话已归属其他账号",
    BLOCK_DUPLICATE: "已经在队列里",
}

CONTACTED_STATES = (OUTREACH_CONTACTED, OUTREACH_REPLIED, OUTREACH_REFUSED)


def has_direct_contact(lead: Lead) -> bool:
    """是否具备发信息账号能用的寻址方式。

    ``PEER_REFERENCE`` / ``SHARED_GROUP`` 绑在监听账号上，发送账号换了人就找不到，
    因此只有用户名 / 手机号 / 正文里留的联系方式才算数。
    """
    routes = set(parse_reachable_routes(lead.reachable_routes))
    if ROUTE_USERNAME in routes or ROUTE_PHONE in routes:
        return True
    return _contacts_has_content(lead.contacts)


def _contacts_has_content(raw: str | None) -> bool:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return any(payload.get(key) for key in ("phones", "wechats", "usernames"))


async def get_or_create_contact(session: AsyncSession, lead: Lead) -> OutreachContact:
    """按「租户 + Telegram 用户 ID」拿联系档案；没有就建一份。"""
    filters = [OutreachContact.tg_user_id == lead.sender_tg_id]
    if lead.tenant_id is not None:
        filters.append(OutreachContact.tenant_id == lead.tenant_id)
    contact = await session.scalar(select(OutreachContact).where(*filters))
    if contact is not None:
        contact.username = lead.sender_username or contact.username
        contact.display_name = lead.sender_name or contact.display_name
        contact.phone = lead.phone or contact.phone
        return contact

    profile_filters = [MemberProfile.tg_user_id == lead.sender_tg_id]
    if lead.tenant_id is not None:
        profile_filters.append(MemberProfile.tenant_id == lead.tenant_id)
    profile_id = await session.scalar(select(MemberProfile.id).where(*profile_filters))

    contact = OutreachContact(
        tg_user_id=lead.sender_tg_id,
        username=lead.sender_username,
        display_name=lead.sender_name,
        phone=lead.phone,
        first_lead_id=lead.id,
        member_profile_id=profile_id,
        identity_confirmed=True,
    )
    if lead.tenant_id is not None:
        contact.tenant_id = lead.tenant_id
    session.add(contact)
    await session.flush()
    return contact


async def evaluate_lead(
    session: AsyncSession,
    lead: Lead,
    contact: OutreachContact,
    settings: dict,
) -> str | None:
    """返回阻塞原因；``None`` 表示可以入队。"""
    if settings.get("kill_switch"):
        return BLOCK_KILL_SWITCH
    if not lead.sender_tg_id:
        return BLOCK_IDENTITY
    if not has_direct_contact(lead):
        return BLOCK_NO_CONTACT
    if contact.do_not_contact or await _is_suppressed(session, contact):
        return BLOCK_DO_NOT_CONTACT
    if contact.first_contact_at is not None or contact.contact_state in CONTACTED_STATES:
        return BLOCK_ALREADY_CONTACTED
    lock_until = as_utc(contact.global_lock_until)
    if lock_until is not None and lock_until > utc_now():
        return BLOCK_GLOBAL_LOCK
    if contact.owner_account_id or contact.owner_bot_id:
        return BLOCK_OWNER
    return None


async def _is_suppressed(session: AsyncSession, contact: OutreachContact) -> bool:
    filters = [ContactSuppression.tg_user_id == contact.tg_user_id]
    if contact.tenant_id is not None:
        filters.append(ContactSuppression.tenant_id == contact.tenant_id)
    found = await session.scalar(select(ContactSuppression.id).where(*filters))
    return found is not None


async def enqueue_lead(
    session: AsyncSession,
    lead: Lead,
    *,
    settings: dict | None = None,
) -> tuple[OutreachTask | None, str]:
    """把一条线索变成排队任务；返回 ``(任务, 阻塞原因)``。"""
    tenant_id = lead.tenant_id or scoped_tenant_id()
    if settings is None:
        settings = await outreach_settings_service.read_settings(session, tenant_id)
    contact = await get_or_create_contact(session, lead)
    reason = await evaluate_lead(session, lead, contact, settings)
    if reason:
        return None, reason

    dedupe_key = f"outreach:first:{contact.id}"
    existing = await session.scalar(
        select(OutreachTask.id).where(OutreachTask.dedupe_key == dedupe_key)
    )
    if existing is not None:
        return None, BLOCK_DUPLICATE

    task = OutreachTask(
        tenant_id=tenant_id,
        lead_id=lead.id,
        contact_id=contact.id,
        kind=TASK_FIRST_CONTACT,
        status=TASK_QUEUED,
        dedupe_key=dedupe_key,
        scheduled_at=utc_now(),
    )
    if lead.tenant_id is not None:
        task.tenant_id = lead.tenant_id
    session.add(task)
    contact.contact_state = CONTACT_QUEUED
    lead.outreach_status = CONTACT_QUEUED
    await session.commit()
    await session.refresh(task)
    return task, ""


async def plan_pending(
    session: AsyncSession,
    *,
    tenant_id: int,
    limit: int = 200,
) -> dict:
    """dry-run 调度：扫描「待发信息账号」的线索，逐条过闸入队。"""
    settings = await outreach_settings_service.read_settings(session, tenant_id)
    statement = (
        select(Lead)
        .where(
            Lead.tenant_id == tenant_id,
            Lead.outreach_status == OUTREACH_WAITING_SENDER_ACCOUNT,
            Lead.sender_tg_id.is_not(None),
        )
        .order_by(Lead.id)
        .limit(limit)
    )
    leads = list(await session.scalars(statement))
    created = 0
    blocked: dict[str, int] = {}
    for lead in leads:
        task, reason = await enqueue_lead(session, lead, settings=settings)
        if task is not None:
            created += 1
        elif reason:
            blocked[reason] = blocked.get(reason, 0) + 1
    return {
        "scanned": len(leads),
        "created": created,
        "blocked": [
            {"reason": reason, "label": BLOCK_LABELS.get(reason, reason), "count": count}
            for reason, count in sorted(blocked.items(), key=lambda item: -item[1])
        ],
    }


async def pending_tenants(session: AsyncSession, *, limit: int = 20) -> list[int]:
    """还有排队任务的租户（供冷触达循环调度）。"""
    rows = await session.scalars(
        select(OutreachTask.tenant_id)
        .where(OutreachTask.status == TASK_QUEUED)
        .distinct()
        .limit(limit)
    )
    return [int(item) for item in rows]


async def next_ready_task(
    session: AsyncSession,
    tenant_id: int,
    *,
    now: datetime | None = None,
) -> OutreachTask | None:
    """取该租户下一个可发送的任务（到点且未被退避推迟）。"""
    moment = now or utc_now()
    statement = (
        select(OutreachTask)
        .where(
            OutreachTask.tenant_id == tenant_id,
            OutreachTask.status == TASK_QUEUED,
            (OutreachTask.next_retry_at.is_(None)) | (OutreachTask.next_retry_at <= moment),
        )
        .order_by(OutreachTask.id)
        .limit(1)
    )
    return await session.scalar(statement)


async def clear_queue(session: AsyncSession, *, tenant_id: int) -> dict[str, int]:
    """清空冷触达队列。

    删掉该租户的全部投递任务；只「排过队、还没真正联系过」的联系人与线索
    退回「待发信息账号」，这样还能重新「生成队列」。已联系/已回复/已拒绝的
    联系人属于历史，不动。
    """
    from sqlalchemy import delete, update

    deleted = await session.execute(delete(OutreachTask).where(OutreachTask.tenant_id == tenant_id))

    contacts = list(
        await session.scalars(
            select(OutreachContact).where(
                OutreachContact.tenant_id == tenant_id,
                OutreachContact.contact_state == CONTACT_QUEUED,
            )
        )
    )
    for contact in contacts:
        contact.contact_state = OUTREACH_WAITING_SENDER_ACCOUNT

    reset_leads = await session.execute(
        update(Lead)
        .where(Lead.tenant_id == tenant_id, Lead.outreach_status == CONTACT_QUEUED)
        .values(outreach_status=OUTREACH_WAITING_SENDER_ACCOUNT)
    )

    await session.commit()
    return {
        "deleted_tasks": int(deleted.rowcount or 0),
        "reset_contacts": len(contacts),
        "reset_leads": int(reset_leads.rowcount or 0),
    }


async def capacity(session: AsyncSession, *, tenant_id: int) -> dict:
    """今日可用冷聊总量、排队数与受限账号数。"""
    settings = await outreach_settings_service.read_settings(session, tenant_id)
    accounts = list(
        await session.scalars(
            select(TgAccount).where(
                TgAccount.tenant_id == tenant_id,
                TgAccount.purpose == ACCOUNT_PURPOSE_OUTREACH,
            )
        )
    )
    available = 0
    for account in accounts:
        snapshot = await outreach_account_service.snapshot(session, account)
        if snapshot["state"] not in (STATE_LIMITED, STATE_PAUSED, STATE_DISABLED):
            available += int(snapshot["remaining"])

    pool_cap = settings.get("daily_pool_cap")
    if pool_cap:
        available = min(available, int(pool_cap))

    states = list(
        await session.scalars(
            select(OutreachAccountState).where(OutreachAccountState.tenant_id == tenant_id)
        )
    )
    queued = int(
        await session.scalar(
            select(func.count())
            .select_from(OutreachTask)
            .where(
                OutreachTask.tenant_id == tenant_id,
                OutreachTask.status.in_((TASK_QUEUED, TASK_ASSIGNED)),
            )
        )
        or 0
    )
    return {
        "accounts": len(accounts),
        "usable_accounts": sum(1 for account in accounts if account.status == ACCOUNT_ACTIVE),
        "today_available": available,
        "queued": queued,
        "cooling_accounts": sum(1 for row in states if row.state in (STATE_COOLING, STATE_CAPPED)),
        "limited_accounts": sum(1 for row in states if row.state == STATE_LIMITED),
        "estimated_days": None
        if not available
        else (0 if queued <= available else ceil(queued / available)),
        "daily_pool_cap": pool_cap,
    }
