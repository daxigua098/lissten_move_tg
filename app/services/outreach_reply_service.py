"""冷触达 B 模式：自动回复。

默认关闭；只对**已回复**的会话生效，只由归属账号发送，绝不用于首触。
命中敏感/负面/询价词、达到轮次上限、人工接管过，都直接转人工。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.outreach_capture import OUTREACH_REPLIED
from app.db.base import as_utc, utc_now
from app.db.models import (
    DIRECTION_OUT,
    MESSAGE_GENERATED_AUTO,
    REPLY_MODE_AUTO,
    REPLY_STATE_HUMAN,
    REPLY_STATE_REPLIED,
    TEMPLATE_AUTO_REPLY,
    OutreachContact,
    OutreachMessage,
    OutreachTemplate,
    TgAccount,
)
from app.services import outreach_sender_service as sender
from app.services import outreach_settings_service

# 命中这些词就转人工，别让机器人乱答
HANDOVER_KEYWORDS = (
    "价格",
    "多少钱",
    "报价",
    "费用",
    "便宜",
    "优惠",
    "投诉",
    "举报",
    "骗子",
    "诈骗",
    "退款",
    "退钱",
    "维权",
    "报警",
    "律师",
    "起诉",
    "违法",
)

AUTO_REPLY_COOLDOWN_SECONDS = 60

BLOCK_DISABLED = "AUTO_REPLY_DISABLED"
BLOCK_MODE = "REPLY_MODE_HUMAN"
BLOCK_HUMAN_TAKEN = "HUMAN_TAKEN"
BLOCK_NOT_REPLIED = "NOT_REPLIED"
BLOCK_MAX_ROUNDS = "MAX_ROUNDS"
BLOCK_COOLDOWN = "COOLDOWN"
BLOCK_NO_TEMPLATE = "NO_TEMPLATE"
BLOCK_NEEDS_HUMAN = "NEEDS_HUMAN"

BLOCK_LABELS: dict[str, str] = {
    BLOCK_DISABLED: "自动回复未开启",
    BLOCK_MODE: "当前是人工接管模式",
    BLOCK_HUMAN_TAKEN: "已转人工",
    BLOCK_NOT_REPLIED: "会话还没有回复",
    BLOCK_MAX_ROUNDS: "已达自动回复轮次上限",
    BLOCK_COOLDOWN: "发送太频繁，稍后再答",
    BLOCK_NO_TEMPLATE: "没有可用的自动回复话术",
    BLOCK_NEEDS_HUMAN: "命中需要人工处理的话题",
}


def needs_handover(text: str | None) -> bool:
    """是否命中需要人工接管的话题。"""
    body = (text or "").lower()
    return any(word in body for word in HANDOVER_KEYWORDS)


async def auto_reply_rounds(session: AsyncSession, contact_id: int) -> int:
    """这个会话已经自动回复过几轮。"""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(OutreachMessage)
            .where(
                OutreachMessage.contact_id == contact_id,
                OutreachMessage.direction == DIRECTION_OUT,
                OutreachMessage.generated_by == MESSAGE_GENERATED_AUTO,
            )
        )
        or 0
    )


async def pick_auto_template(
    session: AsyncSession,
    tenant_id: int,
    settings: dict,
) -> OutreachTemplate | None:
    """优先用设置里指定的模板，其次用任意启用的自动回复模板。"""
    template_id = settings.get("auto_reply_template_id")
    if template_id:
        row = await session.get(OutreachTemplate, template_id)
        if row is not None and row.enabled:
            return row
    return await sender.pick_template(session, tenant_id, TEMPLATE_AUTO_REPLY)


async def should_auto_reply(
    session: AsyncSession,
    *,
    settings: dict,
    contact: OutreachContact,
    now: datetime | None = None,
) -> str | None:
    """返回不能自动回复的原因；``None`` 表示可以回。"""
    moment = now or utc_now()
    if not settings.get("auto_reply_enabled"):
        return BLOCK_DISABLED
    if settings.get("reply_mode") != REPLY_MODE_AUTO:
        return BLOCK_MODE
    if contact.do_not_contact:
        return BLOCK_HUMAN_TAKEN
    if contact.reply_state != REPLY_STATE_REPLIED:
        return BLOCK_HUMAN_TAKEN
    if contact.contact_state != OUTREACH_REPLIED:
        return BLOCK_NOT_REPLIED
    if await auto_reply_rounds(session, contact.id) >= int(
        settings.get("auto_reply_max_rounds") or 0
    ):
        return BLOCK_MAX_ROUNDS
    last = as_utc(contact.last_outbound_at)
    if last is not None and last + timedelta(seconds=AUTO_REPLY_COOLDOWN_SECONDS) > moment:
        return BLOCK_COOLDOWN
    if await pick_auto_template(session, contact.tenant_id, settings) is None:
        return BLOCK_NO_TEMPLATE
    return None


async def auto_reply(
    session: AsyncSession,
    *,
    client: Any,
    account: TgAccount,
    contact: OutreachContact,
    incoming_text: str | None,
    now: datetime | None = None,
    config: AppConfig | None = None,
) -> dict[str, Any]:
    """按 B 模式回一条；命中人工话题或任一闸门不过就跳过。"""
    if needs_handover(incoming_text):
        contact.reply_state = REPLY_STATE_HUMAN
        await session.commit()
        logger.info("联系人 #{} 命中人工话题，已转人工", contact.id)
        return {"status": "HANDOVER", "reason": BLOCK_NEEDS_HUMAN}

    settings = await outreach_settings_service.read_settings(session, contact.tenant_id)
    reason = await should_auto_reply(session, settings=settings, contact=contact, now=now)
    if reason:
        return {"status": "SKIPPED", "reason": reason}

    template = await pick_auto_template(session, contact.tenant_id, settings)
    if template is None:
        return {"status": "SKIPPED", "reason": BLOCK_NO_TEMPLATE}

    moment = now or utc_now()
    text = sender.render_template(template.text, contact)
    entity = await sender.resolve_entity(client, contact)
    message = await sender.send_template(
        client, entity, template=template, text=text, config=config
    )

    contact.last_outbound_at = moment
    session.add(
        OutreachMessage(
            tenant_id=contact.tenant_id,
            contact_id=contact.id,
            account_id=account.id,
            direction=DIRECTION_OUT,
            tg_message_id=int(getattr(message, "id", 0) or 0),
            text=text,
            generated_by=MESSAGE_GENERATED_AUTO,
            sent_at=moment,
        )
    )
    await session.commit()
    logger.info("冷触达自动回复：联系人 #{}（账号 {}）", contact.id, account.name)
    return {"status": "SENT", "message_id": int(getattr(message, "id", 0) or 0)}


async def resume_auto(session: AsyncSession, contact: OutreachContact) -> OutreachContact:
    """人工处理完，重新把这个会话交回自动回复。"""
    contact.reply_state = REPLY_STATE_REPLIED
    await session.commit()
    await session.refresh(contact)
    return contact
