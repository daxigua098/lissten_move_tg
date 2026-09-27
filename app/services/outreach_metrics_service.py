"""冷触达账号的 7 日指标与自动降档。

指标口径：
- 成功率 = 近 7 天「已发送」任务 / （已发送 + 已失败）；
- 回复率 = 近 7 天首次联系过的人里，回过消息的比例；
- 在跟会话数 = 当前归属该账号、且没被拒绝 / 冻结的会话。
样本太少（不足 10 人）不降档，避免误伤新号。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outreach_capture import OUTREACH_REFUSED
from app.db.base import utc_now
from app.db.models import (
    CONTACT_FROZEN,
    TASK_FAILED,
    TASK_SENT,
    TIER_LABELS,
    TIER_MATURE,
    TIER_NEW,
    TIER_STANDARD,
    TIER_WARMING,
    OutreachContact,
    OutreachTask,
    TgAccount,
)
from app.services import outreach_account_service

WINDOW_DAYS = 7
MIN_SAMPLES = 10
DOWNGRADE_REPLY_RATE = 0.05
# 从高到低；降档就往后挪一档
TIER_ORDER = (TIER_MATURE, TIER_STANDARD, TIER_WARMING, TIER_NEW)


async def refresh(
    session: AsyncSession,
    account: TgAccount,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """重算这个账号的近 7 天指标，必要时自动降档。"""
    moment = now or utc_now()
    since = moment - timedelta(days=WINDOW_DAYS)

    sent = await _count(
        session,
        OutreachTask,
        OutreachTask.account_id == account.id,
        OutreachTask.status == TASK_SENT,
        OutreachTask.sent_at.is_not(None),
        OutreachTask.sent_at >= since,
    )
    failed = await _count(
        session,
        OutreachTask,
        OutreachTask.account_id == account.id,
        OutreachTask.status == TASK_FAILED,
        OutreachTask.updated_at >= since,
    )
    total = sent + failed
    success_rate = round(sent / total, 4) if total else None

    contacted = await _count(
        session,
        OutreachContact,
        OutreachContact.first_contact_account_id == account.id,
        OutreachContact.first_contact_at.is_not(None),
        OutreachContact.first_contact_at >= since,
    )
    replied = await _count(
        session,
        OutreachContact,
        OutreachContact.first_contact_account_id == account.id,
        OutreachContact.last_inbound_at.is_not(None),
        OutreachContact.first_contact_at >= since,
    )
    reply_rate = round(replied / contacted, 4) if contacted else None

    active = await _count(
        session,
        OutreachContact,
        OutreachContact.owner_account_id == account.id,
        OutreachContact.contact_state.not_in((CONTACT_FROZEN, OUTREACH_REFUSED)),
    )

    state = await outreach_account_service.get_or_create_state(session, account)
    state.success_rate_7d = success_rate
    state.reply_rate_7d = reply_rate
    state.active_conversation_count = active
    state.metrics_at = moment
    downgraded = _maybe_downgrade(state, samples=contacted, reply_rate=reply_rate)
    await session.commit()
    if downgraded:
        logger.warning("账号 {} 近 7 天回复率偏低，已自动降档到 {}", account.name, downgraded)
    return {
        "account_id": account.id,
        "success_rate_7d": success_rate,
        "reply_rate_7d": reply_rate,
        "active_conversation_count": active,
        "downgraded_to": downgraded,
    }


def _maybe_downgrade(state: Any, *, samples: int, reply_rate: float | None) -> str | None:
    """样本足够且回复率过低时降一档；已经是最低档就不再降。"""
    if samples < MIN_SAMPLES or reply_rate is None or reply_rate >= DOWNGRADE_REPLY_RATE:
        return None
    if state.tier not in TIER_ORDER:
        return None
    index = TIER_ORDER.index(state.tier)
    if index >= len(TIER_ORDER) - 1:
        return None
    state.tier = TIER_ORDER[index + 1]
    state.note = (
        f"近 7 天回复率 {reply_rate:.0%}，已自动降档到 {TIER_LABELS.get(state.tier, state.tier)}"
    )
    return state.tier


async def _count(session: AsyncSession, model: Any, *conditions: Any) -> int:
    statement = select(func.count()).select_from(model)
    for condition in conditions:
        statement = statement.where(condition)
    return int(await session.scalar(statement) or 0)
