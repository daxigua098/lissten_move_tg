"""冷触达发送执行：选号、发送、错误映射与回复处理。

只发首条招呼与跟进；不做自动对话（自动回复是后续阶段的可选项）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.errors import ValidationFailedError
from app.core.outreach_capture import (
    OUTREACH_CONTACTED,
    OUTREACH_REFUSED,
    OUTREACH_REPLIED,
)
from app.db.base import as_utc, utc_now
from app.db.models import (
    ACCOUNT_ACTIVE,
    ACCOUNT_PURPOSE_OUTREACH,
    CONTACT_FROZEN,
    DIRECTION_IN,
    DIRECTION_OUT,
    MESSAGE_GENERATED_AUTO,
    MESSAGE_GENERATED_CONTACT,
    OWNER_ACCOUNT,
    REPLY_STATE_HUMAN,
    REPLY_STATE_REFUSED,
    REPLY_STATE_REPLIED,
    STATE_COOLING,
    STATE_DISABLED,
    STATE_LIMITED,
    STATE_PAUSED,
    TASK_BLOCKED,
    TASK_CANCELLED,
    TASK_FAILED,
    TASK_FIRST_CONTACT,
    TASK_FOLLOW_UP,
    TASK_QUEUED,
    TASK_SENT,
    TASK_UNKNOWN_DELIVERY,
    TEMPLATE_SCOPE_PLATFORM,
    ContactSuppression,
    OutreachAccountDaily,
    OutreachAccountState,
    OutreachContact,
    OutreachMessage,
    OutreachTask,
    OutreachTemplate,
    TgAccount,
)
from app.services import (
    outreach_account_service,
    outreach_queue_service,
    outreach_settings_service,
)

# 对方明确表示不想被联系的说法（中英都覆盖，避免被当成"可以继续聊"）
REFUSAL_KEYWORDS = (
    "不要",
    "不需要",
    "不用了",
    "别再联系",
    "不要再",
    "停止联系",
    "拉黑",
    "投诉",
    "举报",
    "stop",
    "unsubscribe",
    "no thanks",
)

BACKOFF_SECONDS = 300
FLOOD_OBSERVATION_HOURS = 24
LIMITED_DAYS = 3
NO_TEMPLATE_RETRY_SECONDS = 600


def detect_refusal(text: str | None) -> bool:
    """对方是否明确拒绝继续沟通。"""
    body = (text or "").strip().lower()
    return any(word in body for word in REFUSAL_KEYWORDS)


def classify_error(exc: BaseException) -> str:
    """把 Telegram 异常收敛成几类，决定账号与任务怎么处理。"""
    name = exc.__class__.__name__
    text = str(exc).lower()
    if name == "FloodWaitError" or "flood_wait" in text:
        return "flood"
    if name == "PeerFloodError" or "peer_flood" in text:
        return "peer_flood"
    if name in {
        "UserDeactivatedBanError",
        "UserDeactivatedError",
        "AuthKeyUnregisteredError",
        "SessionRevokedError",
        "SessionExpiredError",
    }:
        return "banned"
    if name == "UserPrivacyRestrictedError" or "privacy" in text:
        return "privacy"
    if name in {"UsernameNotOccupiedError", "UsernameInvalidError", "CannotResolveUsernameError"}:
        return "unreachable"
    if name in {
        "ConnectionError",
        "ConnectionResetError",
        "TimeoutError",
        "TimedOutError",
        "DisconnectedError",
    } or any(
        token in text for token in ("timed out", "timeout", "connection reset", "network error")
    ):
        return "uncertain"
    return "unknown"


async def pick_account(
    session: AsyncSession,
    tenant_id: int,
    *,
    now: datetime | None = None,
) -> TgAccount | None:
    """挑一个现在就能用的发信息账号（额度、冷却、受限都要过）。"""
    moment = now or utc_now()
    settings = await outreach_settings_service.read_settings(session, tenant_id)
    tz_name = settings.get("timezone")
    accounts = list(
        await session.scalars(
            select(TgAccount)
            .where(
                TgAccount.tenant_id == tenant_id,
                TgAccount.purpose == ACCOUNT_PURPOSE_OUTREACH,
                TgAccount.status == ACCOUNT_ACTIVE,
            )
            .order_by(TgAccount.id)
        )
    )
    candidates: list[tuple[tuple, TgAccount]] = []
    for account in accounts:
        state = await session.get(OutreachAccountState, account.id)
        if state is not None:
            if state.state in (STATE_LIMITED, STATE_PAUSED, STATE_DISABLED):
                continue
            wait_until = as_utc(state.flood_wait_until)
            if wait_until is not None and wait_until > moment:
                continue
            if _cooling(state, moment):
                continue
        snapshot = await outreach_account_service.snapshot(
            session,
            account,
            now=moment,
            tz_name=tz_name,
        )
        if int(snapshot["remaining"]) <= 0:
            continue
        tier_rank = {"MATURE": 4, "STANDARD": 3, "WARMING": 2, "NEW": 1}
        last_cold = as_utc(state.last_cold_at) if state is not None else None
        score = (
            tier_rank.get(state.tier if state is not None else "NEW", 0),
            float(snapshot.get("success_rate_7d") or 0.5),
            float(snapshot.get("reply_rate_7d") or 0.0),
            int(snapshot["remaining"]),
            -int(snapshot.get("active_conversation_count") or 0),
            -(last_cold.timestamp() if last_cold else 0.0),
            -account.id,
        )
        candidates.append((score, account))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


async def enforce_send_window(
    session: AsyncSession,
    tenant_id: int,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """发送端硬闸门：工作时段与租户日总量。"""
    moment = now or utc_now()
    settings = await outreach_settings_service.read_settings(session, tenant_id)
    tz_name = settings.get("timezone")
    if not outreach_settings_service.within_working_hours(settings, moment, tz_name=tz_name):
        return False, "OUTSIDE_WORKING_HOURS"
    cap = settings.get("daily_pool_cap")
    if not cap:
        return True, ""
    day = outreach_account_service.local_day(moment, tz_name=tz_name)
    sent = int(
        await session.scalar(
            select(func.coalesce(func.sum(OutreachAccountDaily.first_contact_sent), 0)).where(
                OutreachAccountDaily.tenant_id == tenant_id,
                OutreachAccountDaily.day == day,
            )
        )
        or 0
    )
    if sent >= int(cap):
        return False, "DAILY_POOL_CAP"
    return True, ""


def _cooling(state: OutreachAccountState, moment: datetime) -> bool:
    if state.last_cold_at is None:
        return False
    seconds = state.cooldown_seconds or outreach_account_service.tier_limits(state.tier)[1]
    until = as_utc(state.last_cold_at) + timedelta(seconds=int(seconds))
    return until > moment


async def pick_template(
    session: AsyncSession,
    tenant_id: int,
    kind: str,
) -> OutreachTemplate | None:
    """优先用会员自己的模板，其次用平台共享模板。"""
    statement = (
        select(OutreachTemplate)
        .where(
            OutreachTemplate.kind == kind,
            OutreachTemplate.enabled.is_(True),
            or_(
                OutreachTemplate.tenant_id == tenant_id,
                OutreachTemplate.scope == TEMPLATE_SCOPE_PLATFORM,
            ),
        )
        .order_by(OutreachTemplate.scope.desc(), OutreachTemplate.id)
    )
    return await session.scalar(statement)


def render_template(text: str, contact: OutreachContact) -> str:
    """替换模板里的称呼变量。"""
    name = contact.display_name or contact.username or "你好"
    return (text or "").replace("{称呼}", name).replace("{姓名}", name)


async def resolve_entity(client: Any, contact: OutreachContact) -> Any:
    """把联系档案解析成 Telegram 实体：优先用户名，其次用户 ID。"""
    if contact.username:
        try:
            return await client.get_entity(contact.username)
        except Exception:  # noqa: BLE001 - 用户名失效时退回用户 ID
            logger.debug("按用户名解析失败，改用用户 ID：{}", contact.username)
    if contact.tg_user_id:
        return await client.get_entity(int(contact.tg_user_id))
    raise ValidationFailedError("缺少可直接寻址方式，无法发送")


def template_media_path(config: AppConfig | None, template: OutreachTemplate) -> Path | None:
    """模板附带的图片/视频路径（没有则 None）。"""
    if not template.media_path:
        return None
    return config.path(template.media_path) if config is not None else Path(template.media_path)


async def send_template(
    client: Any,
    entity: Any,
    *,
    template: OutreachTemplate,
    text: str,
    config: AppConfig | None = None,
) -> Any:
    """按模板发送：有媒体就带 caption 发文件，否则发纯文本。"""
    path = template_media_path(config, template)
    if path is not None and path.is_file():
        return await client.send_file(entity, str(path), caption=text or None)
    return await client.send_message(entity, text or "")


async def send_task(
    session: AsyncSession,
    *,
    client: Any,
    account: TgAccount,
    task: OutreachTask,
    contact: OutreachContact,
    now: datetime | None = None,
    config: AppConfig | None = None,
) -> dict[str, Any]:
    """发送一条冷触达任务并记账。"""
    moment = now or utc_now()
    task.account_id = account.id
    settings = await outreach_settings_service.read_settings(session, contact.tenant_id)
    allowed, blocked_reason = await enforce_send_window(
        session,
        contact.tenant_id,
        now=moment,
    )
    if not allowed:
        task.status = TASK_QUEUED
        task.account_id = None
        task.next_retry_at = (
            moment + timedelta(hours=1)
            if blocked_reason == "DAILY_POOL_CAP"
            else outreach_settings_service.next_working_start(
                settings,
                moment,
                tz_name=settings.get("timezone"),
            )
        )
        task.last_error = blocked_reason
        await session.commit()
        return {"task_id": task.id, "status": task.status, "error": blocked_reason}
    template = await pick_template(session, contact.tenant_id, task.kind)
    if template is None:
        task.last_error = "没有可用话术模板，请先在「话术模板」里建一套"
        task.next_retry_at = moment + timedelta(seconds=NO_TEMPLATE_RETRY_SECONDS)
        await session.commit()
        return {"task_id": task.id, "status": task.status, "error": "NO_TEMPLATE"}

    text = render_template(template.text, contact)
    task.template_id = template.id
    task.rendered_text = text
    try:
        entity = await resolve_entity(client, contact)
        message = await send_template(client, entity, template=template, text=text, config=config)
    except Exception as exc:  # noqa: BLE001 - 失败原因要落库并决定账号状态
        return await _on_send_error(session, task=task, account=account, contact=contact, exc=exc)

    task.status = TASK_SENT
    task.sent_at = moment
    task.account_id = account.id
    task.target_message_id = _message_id(message)
    task.last_error = None
    task.next_retry_at = None

    contact.contact_state = OUTREACH_CONTACTED
    contact.contact_count += 1
    contact.last_contact_at = moment
    contact.last_outbound_at = moment
    if contact.first_contact_at is None:
        contact.first_contact_at = moment
        contact.first_contact_account_id = account.id
    contact.global_lock_until = outreach_settings_service.lock_until(settings, moment)
    await outreach_queue_service.apply_contact_status_to_leads(session, contact, OUTREACH_CONTACTED)

    state = await outreach_account_service.get_or_create_state(session, account)
    state.last_cold_at = moment
    state.state = STATE_COOLING
    params = {"follow_up": 1} if task.kind == TASK_FOLLOW_UP else {"first_contact": 1}
    await outreach_account_service.bump_daily(
        session,
        account,
        tz_name=settings.get("timezone"),
        **params,
    )
    session.add(
        OutreachMessage(
            tenant_id=contact.tenant_id,
            contact_id=contact.id,
            account_id=account.id,
            direction=DIRECTION_OUT,
            tg_message_id=_message_id(message),
            text=text,
            generated_by=MESSAGE_GENERATED_AUTO,
            sent_at=moment,
        )
    )
    await session.commit()
    logger.info("冷触达已发送：联系人 #{}（账号 {}）", contact.id, account.name)
    return {"task_id": task.id, "status": task.status, "message_id": _message_id(message)}


def _message_id(message: Any) -> int:
    return int(getattr(message, "id", 0) or 0)


async def _on_send_error(
    session: AsyncSession,
    *,
    task: OutreachTask,
    account: TgAccount,
    contact: OutreachContact,
    exc: BaseException,
) -> dict[str, Any]:
    kind = classify_error(exc)
    moment = utc_now()
    task.attempt_count += 1
    task.last_error = f"{kind}: {exc}"[:500]
    state = await outreach_account_service.get_or_create_state(session, account)

    limited = 0
    if kind == "uncertain":
        task.status = TASK_UNKNOWN_DELIVERY
        task.next_retry_at = None
        await session.commit()
        logger.warning("冷触达发送结果不确定：任务 #{}（{}）", task.id, exc)
        return {"task_id": task.id, "status": task.status, "error": kind}
    if kind == "flood":
        seconds = int(getattr(exc, "seconds", 0) or 0)
        state.state = STATE_COOLING
        state.flood_wait_until = moment + timedelta(
            seconds=seconds + FLOOD_OBSERVATION_HOURS * 3600
        )
        task.status = TASK_QUEUED
        task.next_retry_at = state.flood_wait_until
        limited = 1
    elif kind in {"peer_flood", "banned"}:
        state.state = STATE_LIMITED if kind == "peer_flood" else STATE_DISABLED
        state.limited_until = moment + timedelta(days=LIMITED_DAYS)
        state.limited_reason = kind
        not_started = (
            task.kind == TASK_FIRST_CONTACT
            and contact.first_contact_at is None
            and contact.owner_account_id is None
            and contact.contact_state
            not in (OUTREACH_CONTACTED, OUTREACH_REPLIED, OUTREACH_REFUSED)
        )
        if not_started:
            task.status = TASK_QUEUED
            task.account_id = None
            task.next_retry_at = None
            task.last_error = f"{kind}: 原账号不可用，等待其他健康账号"
            contact.contact_state = "QUEUED"
        else:
            await freeze_account_contacts(session, account, reason=kind)
            task.status = TASK_FAILED
        limited = 1
    elif kind in {"privacy", "unreachable"}:
        # 对方不收陌生私聊 / 找不到人：这条线索永久作废，别再换号试
        contact.do_not_contact = True
        await add_suppression(session, contact, reason=kind)
        task.status = TASK_BLOCKED
        await outreach_queue_service.apply_contact_status_to_leads(session, contact, "BLOCKED")
    elif task.attempt_count >= task.max_attempts:
        task.status = TASK_FAILED
    else:
        task.status = TASK_QUEUED
        task.next_retry_at = moment + timedelta(seconds=BACKOFF_SECONDS * task.attempt_count)

    settings = await outreach_settings_service.read_settings(session, contact.tenant_id)
    await outreach_account_service.bump_daily(
        session,
        account,
        tz_name=settings.get("timezone"),
        failed=1,
        limited=limited,
    )
    await session.commit()
    logger.warning("冷触达发送失败：任务 #{}，原因 {}（{}）", task.id, kind, exc)
    return {"task_id": task.id, "status": task.status, "error": kind}


async def confirm_unknown_delivery(
    session: AsyncSession,
    *,
    task: OutreachTask,
    delivered: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """人工核实发送结果：已送达按成功记账，未送达放回队列。"""
    if task.status != TASK_UNKNOWN_DELIVERY:
        raise ValidationFailedError("只有待核实任务可以确认发送结果")
    contact = await session.get(OutreachContact, task.contact_id)
    if contact is None:
        raise ValidationFailedError("任务联系人不存在")
    moment = now or utc_now()
    if not delivered:
        task.status = TASK_QUEUED
        task.account_id = None
        task.next_retry_at = None
        task.attempt_count = 0
        task.last_error = "人工确认未送达，已重新排队"
        contact.contact_state = "QUEUED"
        await session.commit()
        return {"task_id": task.id, "status": task.status, "delivered": False}

    account = await session.get(TgAccount, task.account_id) if task.account_id else None
    settings = await outreach_settings_service.read_settings(session, contact.tenant_id)
    task.status = TASK_SENT
    task.sent_at = moment
    task.next_retry_at = None
    task.last_error = None
    contact.contact_state = OUTREACH_CONTACTED
    contact.contact_count += 1
    contact.last_contact_at = moment
    contact.last_outbound_at = moment
    if contact.first_contact_at is None:
        contact.first_contact_at = moment
        contact.first_contact_account_id = account.id if account is not None else None
    contact.global_lock_until = outreach_settings_service.lock_until(settings, moment)
    await outreach_queue_service.apply_contact_status_to_leads(session, contact, OUTREACH_CONTACTED)

    if account is not None:
        state = await outreach_account_service.get_or_create_state(session, account)
        state.last_cold_at = moment
        state.state = STATE_COOLING
        params = {"follow_up": 1} if task.kind == TASK_FOLLOW_UP else {"first_contact": 1}
        await outreach_account_service.bump_daily(
            session,
            account,
            tz_name=settings.get("timezone"),
            **params,
        )
    await session.commit()
    return {"task_id": task.id, "status": task.status, "delivered": True}


async def retry_failed_task(session: AsyncSession, *, task: OutreachTask) -> dict[str, Any]:
    """人工重试临时失败任务；永久阻塞任务不允许进入这里。"""
    if task.status != TASK_FAILED:
        raise ValidationFailedError("只有发送失败任务可以人工重试")
    contact = await session.get(OutreachContact, task.contact_id)
    if contact is None:
        raise ValidationFailedError("任务联系人不存在")
    if contact.do_not_contact:
        raise ValidationFailedError("联系人已加入免打扰，不能重试")
    task.status = TASK_QUEUED
    task.account_id = None
    task.attempt_count = 0
    task.next_retry_at = None
    task.last_error = "人工重试，等待重新排队"
    contact.contact_state = "QUEUED"
    await outreach_queue_service.apply_contact_status_to_leads(session, contact, "QUEUED")
    await session.commit()
    return {"task_id": task.id, "status": task.status}


async def freeze_account_contacts(
    session: AsyncSession,
    account: TgAccount,
    *,
    reason: str,
) -> int:
    """账号受限或停用：名下会话全部冻结，绝不转给其他账号。"""
    rows = list(
        await session.scalars(
            select(OutreachContact).where(
                OutreachContact.owner_account_id == account.id,
                OutreachContact.contact_state.not_in((CONTACT_FROZEN, OUTREACH_REFUSED)),
            )
        )
    )
    for row in rows:
        row.contact_state = CONTACT_FROZEN
        row.frozen_reason = reason
    return len(rows)


async def handle_incoming(
    session: AsyncSession,
    *,
    tenant_id: int,
    account_id: int,
    sender_tg_id: int,
    text: str | None,
    tg_message_id: int = 0,
    sent_at: datetime | None = None,
) -> OutreachContact | None:
    """处理一条入站回复：记录 + 归属锁定 / 拒绝拉黑。"""
    contact = await session.scalar(
        select(OutreachContact).where(
            OutreachContact.tenant_id == tenant_id,
            OutreachContact.tg_user_id == sender_tg_id,
        )
    )
    if contact is None:
        return None

    moment = sent_at or utc_now()
    session.add(
        OutreachMessage(
            tenant_id=tenant_id,
            contact_id=contact.id,
            account_id=account_id,
            direction=DIRECTION_IN,
            tg_message_id=int(tg_message_id or 0),
            text=(text or "")[:4000],
            generated_by=MESSAGE_GENERATED_CONTACT,
            sent_at=moment,
        )
    )
    contact.last_inbound_at = moment

    if detect_refusal(text):
        contact.contact_state = OUTREACH_REFUSED
        contact.reply_state = REPLY_STATE_REFUSED
        contact.do_not_contact = True
        await add_suppression(session, contact, reason="refusal")
        await cancel_queued(session, contact.id, reason="对方明确拒绝")
        await outreach_queue_service.apply_contact_status_to_leads(
            session,
            contact,
            OUTREACH_REFUSED,
        )
        logger.info("联系人 #{} 明确拒绝，已加入永久免打扰", contact.id)
    else:
        contact.contact_state = OUTREACH_REPLIED
        contact.reply_state = REPLY_STATE_REPLIED
        contact.owner_type = OWNER_ACCOUNT
        contact.owner_account_id = account_id
        await cancel_queued(session, contact.id, reason="对方已回复，会话归属原账号")
        await outreach_queue_service.apply_contact_status_to_leads(
            session,
            contact,
            OUTREACH_REPLIED,
        )
        logger.info("联系人 #{} 已回复，会话归属账号 #{}", contact.id, account_id)

    await session.commit()
    return contact


async def takeover(
    session: AsyncSession,
    contact: OutreachContact,
) -> OutreachContact:
    """A 模式人工接管：标记为人工处理，不再有任何自动动作。"""
    contact.reply_state = REPLY_STATE_HUMAN
    await session.commit()
    await session.refresh(contact)
    return contact


async def add_suppression(
    session: AsyncSession,
    contact: OutreachContact,
    *,
    reason: str,
    note: str | None = None,
    created_by: str | None = None,
) -> None:
    """加入永久免打扰名单（幂等）。"""
    existing = await session.scalar(
        select(ContactSuppression.id).where(
            ContactSuppression.tenant_id == contact.tenant_id,
            ContactSuppression.tg_user_id == contact.tg_user_id,
        )
    )
    contact.do_not_contact = True
    if existing is None:
        session.add(
            ContactSuppression(
                tenant_id=contact.tenant_id,
                tg_user_id=contact.tg_user_id,
                reason=reason,
                note=note,
                created_by=created_by,
            )
        )


async def remove_suppression(session: AsyncSession, contact: OutreachContact) -> None:
    """解除免打扰（仅人工操作）。"""
    rows = list(
        await session.scalars(
            select(ContactSuppression).where(
                ContactSuppression.tenant_id == contact.tenant_id,
                ContactSuppression.tg_user_id == contact.tg_user_id,
            )
        )
    )
    for row in rows:
        await session.delete(row)
    contact.do_not_contact = False


async def cancel_queued(session: AsyncSession, contact_id: int, *, reason: str) -> None:
    """取消该联系人排队中的任务。"""
    rows = list(
        await session.scalars(
            select(OutreachTask).where(
                OutreachTask.contact_id == contact_id,
                OutreachTask.status == TASK_QUEUED,
            )
        )
    )
    for row in rows:
        row.status = TASK_CANCELLED
        row.last_error = reason
