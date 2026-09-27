"""冷触达 P4：把已回复的会话转交给 Bot。

用户必须点消息里的 ``https://t.me/<bot>?start=<token>`` 并按下 Start，
Bot 才拿到私聊权；首条冷消息禁止放这条链接。
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationFailedError
from app.core.outreach_capture import OUTREACH_REPLIED
from app.db.base import as_utc, utc_now
from app.db.models import (
    DIRECTION_OUT,
    HANDOFF_EXPIRED,
    HANDOFF_PENDING,
    HANDOFF_USED,
    MESSAGE_GENERATED_AUTO,
    OWNER_BOT,
    ControlBot,
    OutreachContact,
    OutreachHandoffToken,
    OutreachMessage,
    TgAccount,
)
from app.services import outreach_sender_service as sender

HANDOFF_TTL_HOURS = 24
HANDOFF_TEMPLATE = "如果想继续了解，点这里进入机器人并按「开始」：{link}"


def build_link(bot_username: str, token: str) -> str:
    """Bot 深链接：用户必须自己点进私聊并按下 Start。"""
    return f"https://t.me/{bot_username}?start={token}"


async def create_handoff(
    session: AsyncSession,
    *,
    contact: OutreachContact,
    settings: dict,
    created_by: str | None = None,
) -> tuple[OutreachHandoffToken, ControlBot, str]:
    """生成一次性转交令牌；只有「用户已回复 + 账号在跟进」的会话能生成。"""
    if not settings.get("handoff_bot_enabled"):
        raise ValidationFailedError("还没有开启 Bot 转交，请先到「策略」里打开并选择承接 Bot")
    bot_id = settings.get("handoff_bot_id")
    if not bot_id:
        raise ValidationFailedError("请先在「策略」里选择承接 Bot")
    bot = await session.get(ControlBot, int(bot_id))
    if bot is None or not bot.bot_username:
        raise ValidationFailedError("承接 Bot 不存在或还没有用户名")
    if contact.contact_state != OUTREACH_REPLIED or contact.reply_state != "REPLIED":
        raise ValidationFailedError("只有用户已经回复、且还在跟进的会话才能转交")
    if not contact.owner_account_id:
        raise ValidationFailedError("该会话还没有归属账号，不能转交")

    moment = utc_now()
    row = OutreachHandoffToken(
        tenant_id=contact.tenant_id,
        token=secrets.token_hex(16),
        contact_id=contact.id,
        bot_id=bot.id,
        status=HANDOFF_PENDING,
        expires_at=moment + timedelta(hours=HANDOFF_TTL_HOURS),
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    return row, bot, build_link(bot.bot_username, row.token)


async def send_handoff(
    session: AsyncSession,
    *,
    client: Any,
    account: TgAccount,
    contact: OutreachContact,
    settings: dict,
    created_by: str | None = None,
) -> dict[str, Any]:
    """把转交链接发给对方（由归属账号发出）。"""
    row, bot, link = await create_handoff(
        session,
        contact=contact,
        settings=settings,
        created_by=created_by,
    )
    text = HANDOFF_TEMPLATE.format(link=link)
    entity = await sender.resolve_entity(client, contact)
    message = await client.send_message(entity, text)

    moment = utc_now()
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
    logger.info("冷触达已发出 Bot 转交链接：联系人 #{}（Bot @{}）", contact.id, bot.bot_username)
    return {
        "contact_id": contact.id,
        "bot_id": bot.id,
        "bot_username": bot.bot_username,
        "link": link,
        "expires_at": as_utc(row.expires_at),
    }


async def consume(
    session: AsyncSession,
    token: str,
    *,
    tg_user_id: int | None = None,
) -> OutreachContact | None:
    """用户按下 Bot 的 Start：令牌作废并把会话归属从账号转给 Bot。"""
    value = (token or "").strip()
    if not value:
        return None
    row = await session.scalar(
        select(OutreachHandoffToken).where(OutreachHandoffToken.token == value)
    )
    if row is None:
        return None
    moment = utc_now()
    if row.status != HANDOFF_PENDING:
        return None
    if as_utc(row.expires_at) <= moment:
        row.status = HANDOFF_EXPIRED
        await session.commit()
        return None

    contact = await session.get(OutreachContact, row.contact_id)
    if contact is None:
        return None
    if tg_user_id is not None and int(tg_user_id) != int(contact.tg_user_id):
        # 令牌被转发给别人：直接作废，避免串号
        row.status = HANDOFF_EXPIRED
        await session.commit()
        return None

    row.status = HANDOFF_USED
    row.used_at = moment
    contact.owner_type = OWNER_BOT
    contact.owner_bot_id = row.bot_id
    contact.owner_account_id = None
    await sender.cancel_queued(session, contact.id, reason="已转交给 Bot")
    await session.commit()
    logger.info("会话已转交给 Bot #{}：联系人 #{}", row.bot_id, contact.id)
    return contact
