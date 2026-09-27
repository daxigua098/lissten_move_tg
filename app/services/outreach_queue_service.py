"""冷触达任务入队：按人聚合、优先级排序、闸门判定与受阻重试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from math import ceil

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outreach_capture import (
    CONSENT_EXPLICIT_DM_INVITE,
    CONSENT_MEMBER,
    CONSENT_NONE,
    CONSENT_PRIOR_REPLY,
    OUTREACH_CONTACTED,
    OUTREACH_REFUSED,
    OUTREACH_REPLIED,
    OUTREACH_WAITING_SENDER_ACCOUNT,
)
from app.db.base import as_utc, utc_now
from app.db.models import (
    ACCOUNT_ACTIVE,
    ACCOUNT_PURPOSE_OUTREACH,
    CONTACT_FROZEN,
    CONTACT_QUEUED,
    MODULE_OUTREACH,
    SELF_TENANT_ID,
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
    OutreachAccountDaily,
    OutreachAccountState,
    OutreachContact,
    OutreachTask,
    Tenant,
    TgAccount,
)
from app.db.tenant_context import scoped_tenant_id
from app.services import (
    outreach_account_service,
    outreach_settings_service,
    tenant_module_service,
    tenant_status_service,
)

BLOCK_TENANT = "TENANT_INACTIVE"
BLOCK_MODULE = "MODULE_DISABLED"
BLOCK_KILL_SWITCH = "KILL_SWITCH"
BLOCK_AGE_EXPIRED = "AGE_EXPIRED"
BLOCK_DO_NOT_CONTACT = "DO_NOT_CONTACT"
BLOCK_IDENTITY = "IDENTITY_UNCONFIRMED"
BLOCK_NO_CONTACT = "NO_USERNAME"
BLOCK_CONSENT = "CONSENT_REQUIRED"
BLOCK_ALREADY_CONTACTED = "ALREADY_CONTACTED"
BLOCK_GLOBAL_LOCK = "GLOBAL_LOCK"
BLOCK_OWNER = "CONVERSATION_OWNER"
BLOCK_FROZEN = "CONTACT_FROZEN"
BLOCK_DUPLICATE = "DUPLICATE"

BLOCK_LABELS: dict[str, str] = {
    BLOCK_TENANT: "租户已过期或停用",
    BLOCK_MODULE: "冷触达功能未开启",
    BLOCK_KILL_SWITCH: "冷触达已熔断",
    BLOCK_AGE_EXPIRED: "线索已超过可触达时效",
    BLOCK_DO_NOT_CONTACT: "在免打扰名单里",
    BLOCK_IDENTITY: "拿不到稳定的 Telegram 用户 ID",
    BLOCK_NO_CONTACT: "缺少可发送的用户名",
    BLOCK_CONSENT: "当前策略要求明确授权",
    BLOCK_ALREADY_CONTACTED: "这个人已经联系过",
    BLOCK_GLOBAL_LOCK: "在跨账号锁定期内",
    BLOCK_OWNER: "会话已归属其他账号",
    BLOCK_FROZEN: "联系人已冻结",
    BLOCK_DUPLICATE: "已经在队列里",
}

CONTACTED_STATES = (OUTREACH_CONTACTED, OUTREACH_REPLIED, OUTREACH_REFUSED)
PERMANENT_RETRY = datetime(9999, 12, 31, tzinfo=UTC)
MAX_SCAN = 10000


def _contacts_payload(raw: str | None) -> dict:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _contacts_usernames(raw: str | None) -> list[str]:
    payload = _contacts_payload(raw)
    values = payload.get("usernames") or []
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for item in values:
        value = str(item or "").strip().lstrip("@")
        if value and value not in result:
            result.append(value)
    return result


def lead_username(lead: Lead) -> str | None:
    """选中线索里最可靠的发送用户名。"""
    sender = (lead.sender_username or "").strip().lstrip("@")
    if sender:
        return sender
    usernames = _contacts_usernames(lead.contacts)
    return usernames[0] if usernames else None


def lead_username_reason(lead: Lead) -> str:
    return "发送者用户名" if (lead.sender_username or "").strip() else "正文联系方式"


def has_direct_contact(lead: Lead) -> bool:
    """手机号 / 微信不能直接发送，当前只认可 Telegram 用户名。"""
    return lead_username(lead) is not None


def lead_priority_score(lead: Lead) -> tuple[int, str]:
    """返回任务优先级分值与可读原因。"""
    score = 0
    reasons: list[str] = []
    consent_points = {
        CONSENT_MEMBER: (400, "会员授权"),
        CONSENT_EXPLICIT_DM_INVITE: (350, "明确邀请"),
        CONSENT_PRIOR_REPLY: (300, "历史回复"),
    }
    value, label = consent_points.get(lead.consent_type, (0, "无明确授权"))
    score += value
    reasons.append(label)

    if lead.keyword:
        score += 200 + min(99, max(0, round(float(lead.score or 0) * 100)))
        reasons.append(f"命中 {lead.keyword}")
    else:
        reasons.append("全量入库")

    if (lead.sender_username or "").strip():
        score += 80
        reasons.append("发送者用户名")
    elif _contacts_usernames(lead.contacts):
        score += 60
        reasons.append("正文用户名")
    return score, " · ".join(reasons)


def _lead_sort_key(lead: Lead) -> tuple[int, datetime, int]:
    moment = as_utc(lead.message_at) or as_utc(lead.created_at) or datetime.min.replace(tzinfo=UTC)
    return lead_priority_score(lead)[0], moment, int(lead.id or 0)


def _retry_at(reason: str, contact: OutreachContact, now: datetime) -> datetime | None:
    if reason in (BLOCK_ALREADY_CONTACTED, BLOCK_DO_NOT_CONTACT, BLOCK_OWNER, BLOCK_FROZEN):
        return PERMANENT_RETRY
    if reason == BLOCK_AGE_EXPIRED:
        return PERMANENT_RETRY
    if reason == BLOCK_DUPLICATE:
        return PERMANENT_RETRY
    if reason == BLOCK_GLOBAL_LOCK:
        lock_until = as_utc(contact.global_lock_until)
        return lock_until if lock_until and lock_until > now else now
    if reason in (BLOCK_TENANT, BLOCK_MODULE, BLOCK_KILL_SWITCH):
        return now + timedelta(hours=1)
    if reason == BLOCK_CONSENT:
        return now + timedelta(days=1)
    if reason in (BLOCK_NO_CONTACT, BLOCK_IDENTITY):
        return now + timedelta(days=1)
    return now + timedelta(hours=6)


async def get_or_create_contact(
    session: AsyncSession,
    lead: Lead,
    *,
    persist: bool = True,
) -> OutreachContact:
    """按「租户 + Telegram 用户 ID」拿联系档案；没有就建一份。"""
    filters = [OutreachContact.tg_user_id == lead.sender_tg_id]
    if lead.tenant_id is not None:
        filters.append(OutreachContact.tenant_id == lead.tenant_id)
    contact = await session.scalar(select(OutreachContact).where(*filters))

    profile_filters = [MemberProfile.tg_user_id == lead.sender_tg_id]
    if lead.tenant_id is not None:
        profile_filters.append(MemberProfile.tenant_id == lead.tenant_id)
    profile_id = await session.scalar(select(MemberProfile.id).where(*profile_filters))
    username = lead_username(lead)

    if contact is not None:
        if username:
            contact.username = username
        contact.display_name = lead.sender_name or contact.display_name
        contact.phone = lead.phone or contact.phone
        contact.member_profile_id = contact.member_profile_id or profile_id
        return contact

    contact = OutreachContact(
        tg_user_id=lead.sender_tg_id,
        username=username,
        display_name=lead.sender_name,
        phone=lead.phone,
        first_lead_id=lead.id,
        member_profile_id=profile_id,
        identity_confirmed=True,
    )
    if lead.tenant_id is not None:
        contact.tenant_id = lead.tenant_id
    if persist:
        session.add(contact)
        await session.flush()
    return contact


async def evaluate_lead(
    session: AsyncSession,
    lead: Lead,
    contact: OutreachContact,
    settings: dict,
    *,
    now: datetime | None = None,
) -> str | None:
    """返回阻塞原因；``None`` 表示可以入队。"""
    moment = now or utc_now()
    tenant_id = int(lead.tenant_id or scoped_tenant_id())
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or not tenant_status_service.is_active(tenant, now=moment):
        return BLOCK_TENANT
    if tenant_id != SELF_TENANT_ID and not await tenant_module_service.has_module(
        session,
        tenant_id,
        MODULE_OUTREACH,
    ):
        return BLOCK_MODULE
    if settings.get("kill_switch"):
        return BLOCK_KILL_SWITCH

    max_age = settings.get("max_lead_age_days")
    age_moment = as_utc(lead.message_at) or as_utc(lead.created_at)
    if max_age and age_moment and age_moment < moment - timedelta(days=int(max_age)):
        return BLOCK_AGE_EXPIRED

    if not lead.sender_tg_id:
        return BLOCK_IDENTITY
    if not has_direct_contact(lead):
        return BLOCK_NO_CONTACT
    if settings.get("only_authorized") and lead.consent_type == CONSENT_NONE:
        return BLOCK_CONSENT
    if contact.do_not_contact or await _is_suppressed(session, contact):
        return BLOCK_DO_NOT_CONTACT
    if contact.contact_state == CONTACT_FROZEN:
        return BLOCK_FROZEN
    if contact.first_contact_at is not None or contact.contact_state in CONTACTED_STATES:
        return BLOCK_ALREADY_CONTACTED

    lock_until = as_utc(contact.global_lock_until)
    if lock_until is not None and lock_until > moment:
        return BLOCK_GLOBAL_LOCK
    if contact.owner_account_id or contact.owner_bot_id:
        return BLOCK_OWNER

    profile = await session.scalar(
        select(MemberProfile).where(
            MemberProfile.tenant_id == tenant_id,
            MemberProfile.tg_user_id == lead.sender_tg_id,
        )
    )
    if profile is not None:
        if profile.conversation_owner_account_id is not None:
            return BLOCK_OWNER
        if profile.first_contact_at is not None or profile.outreach_status in CONTACTED_STATES:
            return BLOCK_ALREADY_CONTACTED
    return None


async def _is_suppressed(session: AsyncSession, contact: OutreachContact) -> bool:
    filters = [ContactSuppression.tg_user_id == contact.tg_user_id]
    if contact.tenant_id is not None:
        filters.append(ContactSuppression.tenant_id == contact.tenant_id)
    found = await session.scalar(select(ContactSuppression.id).where(*filters))
    return found is not None


async def _mark_blocked(
    session: AsyncSession,
    lead: Lead,
    contact: OutreachContact,
    reason: str,
    *,
    now: datetime,
    persist: bool = True,
) -> None:
    if not persist:
        return
    lead.last_plan_checked_at = now
    lead.last_block_reason = reason
    lead.next_plan_at = _retry_at(reason, contact, now)
    await session.flush()


async def enqueue_lead(
    session: AsyncSession,
    lead: Lead,
    *,
    settings: dict | None = None,
    contact: OutreachContact | None = None,
    now: datetime | None = None,
) -> tuple[OutreachTask | None, str]:
    """把一条线索变成排队任务；返回 ``(任务, 阻塞原因)``。"""
    moment = now or utc_now()
    tenant_id = int(lead.tenant_id or scoped_tenant_id())
    if settings is None:
        settings = await outreach_settings_service.read_settings(session, tenant_id)
    if contact is None:
        contact = await get_or_create_contact(session, lead)

    reason = await evaluate_lead(session, lead, contact, settings, now=moment)
    if reason:
        await _mark_blocked(session, lead, contact, reason, now=moment)
        return None, reason

    dedupe_key = f"outreach:first:{contact.id}"
    existing = await session.scalar(
        select(OutreachTask.id).where(OutreachTask.dedupe_key == dedupe_key)
    )
    if existing is not None:
        await _mark_blocked(session, lead, contact, BLOCK_DUPLICATE, now=moment)
        return None, BLOCK_DUPLICATE

    priority, priority_reason = lead_priority_score(lead)
    task = OutreachTask(
        tenant_id=tenant_id,
        lead_id=lead.id,
        contact_id=contact.id,
        kind=TASK_FIRST_CONTACT,
        status=TASK_QUEUED,
        priority=priority,
        priority_reason=priority_reason,
        dedupe_key=dedupe_key,
        scheduled_at=moment,
    )
    session.add(task)
    contact.contact_state = CONTACT_QUEUED
    await session.execute(
        update(Lead)
        .where(
            Lead.tenant_id == tenant_id,
            Lead.sender_tg_id == lead.sender_tg_id,
            Lead.outreach_status == OUTREACH_WAITING_SENDER_ACCOUNT,
        )
        .values(
            outreach_status=CONTACT_QUEUED,
            last_plan_checked_at=moment,
            last_block_reason=None,
            next_plan_at=None,
        )
    )
    await session.commit()
    await session.refresh(task)
    return task, ""


def _filters(
    statement,
    *,
    route_ids: list[int] | None,
    source_chat_ids: list[int] | None,
    keyword: str | None,
    only_hits: bool,
):
    if route_ids:
        statement = statement.where(Lead.route_id.in_(route_ids))
    if source_chat_ids:
        statement = statement.where(Lead.source_chat_id.in_(source_chat_ids))
    if keyword:
        statement = statement.where(Lead.keyword.contains(keyword.strip()))
    if only_hits:
        statement = statement.where(Lead.keyword.is_not(None))
    return statement


async def plan_pending(
    session: AsyncSession,
    *,
    tenant_id: int,
    limit: int = 200,
    route_ids: list[int] | None = None,
    source_chat_ids: list[int] | None = None,
    keyword: str | None = None,
    only_hits: bool = False,
    authorized_only: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict:
    """按人聚合并逐条过闸入队；受阻项不占用成功条数。"""
    moment = now or utc_now()
    settings = await outreach_settings_service.read_settings(session, tenant_id)
    if authorized_only:
        settings = {**settings, "only_authorized": True}

    statement = (
        select(Lead)
        .where(
            Lead.tenant_id == tenant_id,
            Lead.outreach_status == OUTREACH_WAITING_SENDER_ACCOUNT,
            Lead.sender_tg_id.is_not(None),
        )
        .order_by(Lead.created_at.desc(), Lead.id.desc())
        .limit(MAX_SCAN)
    )
    statement = _filters(
        statement,
        route_ids=route_ids,
        source_chat_ids=source_chat_ids,
        keyword=keyword,
        only_hits=only_hits,
    )
    leads = list(await session.scalars(statement))
    grouped: dict[int, list[Lead]] = {}
    for lead in leads:
        grouped.setdefault(int(lead.sender_tg_id or 0), []).append(lead)

    ordered_groups = list(grouped.values())
    ordered_groups.sort(
        key=lambda items: _lead_sort_key(max(items, key=_lead_sort_key)),
        reverse=True,
    )

    created = 0
    blocked: dict[str, int] = {}
    blocked_contacts = 0
    for items in ordered_groups:
        if created >= limit:
            break
        items.sort(key=_lead_sort_key, reverse=True)
        top = items[0]
        next_at = as_utc(top.next_plan_at)
        if next_at is not None and next_at > moment:
            continue

        chosen: Lead | None = None
        chosen_contact: OutreachContact | None = None
        first_reason = ""
        for lead in items:
            contact = await get_or_create_contact(session, lead, persist=not dry_run)
            reason = await evaluate_lead(session, lead, contact, settings, now=moment)
            if not reason:
                chosen = lead
                chosen_contact = contact
                break
            if not first_reason:
                first_reason = reason

        if chosen is None or chosen_contact is None:
            blocked_contacts += 1
            reason = first_reason or BLOCK_DUPLICATE
            blocked[reason] = blocked.get(reason, 0) + 1
            await _mark_blocked(
                session,
                top,
                await get_or_create_contact(session, top, persist=not dry_run),
                reason,
                now=moment,
                persist=not dry_run,
            )
            continue
        if dry_run:
            created += 1
            continue
        task, reason = await enqueue_lead(
            session,
            chosen,
            settings=settings,
            contact=chosen_contact,
            now=moment,
        )
        if task is not None:
            created += 1
        elif reason:
            blocked_contacts += 1
            blocked[reason] = blocked.get(reason, 0) + 1

    if not dry_run:
        await session.commit()
    return {
        "scanned": len(leads),
        "scanned_leads": len(leads),
        "scanned_contacts": len(grouped),
        "created": created,
        "blocked_contacts": blocked_contacts,
        "blocked": [
            {"reason": reason, "label": BLOCK_LABELS.get(reason, reason), "count": count}
            for reason, count in sorted(blocked.items(), key=lambda item: -item[1])
        ],
        "dry_run": dry_run,
    }


async def preview_pending(
    session: AsyncSession, *, tenant_id: int, limit: int = 200, **filters
) -> dict:
    return await plan_pending(session, tenant_id=tenant_id, limit=limit, dry_run=True, **filters)


async def pending_tenants(session: AsyncSession, *, limit: int = 20) -> list[int]:
    """还有排队任务的租户（供冷触达循环调度）。"""
    rows = await session.scalars(
        select(OutreachTask.tenant_id)
        .where(OutreachTask.status == TASK_QUEUED)
        .distinct()
        .limit(limit)
    )
    return [int(item) for item in rows]


def task_effective_priority(task: OutreachTask, now: datetime) -> int:
    scheduled = as_utc(task.scheduled_at) or now
    age_days = max(0, (now - scheduled).days)
    aging = min(70, (age_days // 7) * 10)
    return int(task.priority or 0) + aging


async def next_ready_task(
    session: AsyncSession,
    tenant_id: int,
    *,
    now: datetime | None = None,
) -> OutreachTask | None:
    """取该租户下一个可发送的任务；在稳定优先级上叠加等待提权。"""
    moment = now or utc_now()
    statement = (
        select(OutreachTask)
        .where(
            OutreachTask.tenant_id == tenant_id,
            OutreachTask.status == TASK_QUEUED,
            (OutreachTask.next_retry_at.is_(None)) | (OutreachTask.next_retry_at <= moment),
        )
        .order_by(OutreachTask.scheduled_at, OutreachTask.id)
        .limit(500)
    )
    candidates = list(await session.scalars(statement))
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            task_effective_priority(item, moment),
            -int(item.id),
        ),
    )


async def apply_contact_status_to_leads(
    session: AsyncSession,
    contact: OutreachContact,
    status: str,
) -> int:
    """把一个人所有相关 Lead 同步到联系人的最终业务状态。"""
    result = await session.execute(
        update(Lead)
        .where(
            Lead.tenant_id == contact.tenant_id,
            Lead.sender_tg_id == contact.tg_user_id,
        )
        .values(outreach_status=status, next_plan_at=None)
    )
    return int(result.rowcount or 0)


async def clear_queue(session: AsyncSession, *, tenant_id: int) -> dict[str, int]:
    """只清理待发送任务；已发送 / 失败 / 阻塞历史全部保留。"""
    pending = list(
        await session.scalars(
            select(OutreachTask).where(
                OutreachTask.tenant_id == tenant_id,
                OutreachTask.status.in_((TASK_QUEUED, TASK_ASSIGNED)),
            )
        )
    )
    contact_ids = [row.contact_id for row in pending]
    if contact_ids:
        await session.execute(
            delete(OutreachTask).where(
                OutreachTask.tenant_id == tenant_id,
                OutreachTask.status.in_((TASK_QUEUED, TASK_ASSIGNED)),
            )
        )
    contacts = (
        list(
            await session.scalars(
                select(OutreachContact).where(OutreachContact.id.in_(contact_ids))
            )
        )
        if contact_ids
        else []
    )
    sender_ids = [row.tg_user_id for row in contacts]
    for contact in contacts:
        contact.contact_state = OUTREACH_WAITING_SENDER_ACCOUNT
    reset_leads = 0
    if sender_ids:
        result = await session.execute(
            update(Lead)
            .where(
                Lead.tenant_id == tenant_id,
                Lead.sender_tg_id.in_(sender_ids),
                Lead.outreach_status == CONTACT_QUEUED,
            )
            .values(outreach_status=OUTREACH_WAITING_SENDER_ACCOUNT, next_plan_at=None)
        )
        reset_leads = int(result.rowcount or 0)

    await session.commit()
    return {
        "deleted_tasks": len(pending),
        "reset_contacts": len(contacts),
        "reset_leads": reset_leads,
    }


async def delete_tasks(
    session: AsyncSession,
    task_ids: list[int],
    *,
    tenant_id: int,
) -> dict[str, int]:
    """删除指定任务；只把尚未真正联系过的人退回待生成。"""
    rows = list(
        await session.scalars(
            select(OutreachTask).where(
                OutreachTask.tenant_id == tenant_id,
                OutreachTask.id.in_(task_ids),
            )
        )
    )
    contact_ids = [row.contact_id for row in rows]
    for row in rows:
        await session.delete(row)
    contacts = (
        list(
            await session.scalars(
                select(OutreachContact).where(OutreachContact.id.in_(contact_ids))
            )
        )
        if contact_ids
        else []
    )
    for contact in contacts:
        if contact.first_contact_at is None and contact.contact_state == CONTACT_QUEUED:
            contact.contact_state = OUTREACH_WAITING_SENDER_ACCOUNT
    sender_ids = [
        contact.tg_user_id
        for contact in contacts
        if contact.contact_state == OUTREACH_WAITING_SENDER_ACCOUNT
    ]
    reset_leads = 0
    if sender_ids:
        result = await session.execute(
            update(Lead)
            .where(
                Lead.tenant_id == tenant_id,
                Lead.sender_tg_id.in_(sender_ids),
                Lead.outreach_status == CONTACT_QUEUED,
            )
            .values(outreach_status=OUTREACH_WAITING_SENDER_ACCOUNT, next_plan_at=None)
        )
        reset_leads = int(result.rowcount or 0)
    await session.commit()
    return {"deleted": len(rows), "reset_leads": reset_leads}


async def capacity(session: AsyncSession, *, tenant_id: int) -> dict:
    """今日可用冷聊总量、排队数与受限账号数。"""
    settings = await outreach_settings_service.read_settings(session, tenant_id)
    tz_name = settings.get("timezone")
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
        snapshot = await outreach_account_service.snapshot(session, account, tz_name=tz_name)
        if snapshot["state"] not in (STATE_LIMITED, STATE_PAUSED, STATE_DISABLED):
            available += int(snapshot["remaining"])

    day = outreach_account_service.local_day(tz_name=tz_name)
    sent_today = int(
        await session.scalar(
            select(func.coalesce(func.sum(OutreachAccountDaily.first_contact_sent), 0)).where(
                OutreachAccountDaily.tenant_id == tenant_id,
                OutreachAccountDaily.day == day,
            )
        )
        or 0
    )
    pool_cap = settings.get("daily_pool_cap")
    if pool_cap:
        available = min(available, max(0, int(pool_cap) - sent_today))

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
        "timezone": tz_name,
    }
