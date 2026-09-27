"""到期提醒（P4-05）：按阶段生成去重提醒，并汇总给后台列表展示。

规则（八项决议第 7 条 + P4-05）：

- **代理**：到期前 7 / 3 / 1 天各提醒一次，文案带"请预留额度"；
- **会员**：到期前 3 / 1 天各提醒一次（登录后横幅 + 后台到期列表）；
- 停用中的租户不再催（停用是处罚，催续费没有意义）；已过期也不再生成；
- 续期后剩余天数回到窗口外，提醒自然从列表消失，历史记录保留可追溯。

渠道：站内（``inapp``）+ TG 通知（``telegram``）。TG 出口取「租户自己的默认
通知 Bot」（控制 Bot 的 ``is_default``），没配就退回自营租户的默认 Bot；收件人是
Bot 里的「管理员 TG 用户 ID」。没配 Bot / 没配管理员 / 发送失败都只留在站内渠道，
不影响到期强停，也不影响其他租户的提醒。
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.bot_api import BotApiClient
from app.core.config import AppConfig
from app.core.expiry import resolve_timezone
from app.db.base import as_utc
from app.db.models import (
    AUDIENCE_AGENT,
    AUDIENCE_MEMBER,
    CHANNEL_INAPP,
    CHANNEL_TELEGRAM,
    SELF_TENANT_ID,
    TENANT_KIND_MEMBER,
    TENANT_STATUS_ACTIVE,
    ControlBot,
    Tenant,
    TenantReminder,
    User,
)
from app.services import bot_service, tenant_status_service

# 代理阶段与会员阶段（天）
AGENT_STAGES = (7, 3, 1)
MEMBER_STAGES = (3, 1)
STAGE_LABELS = {7: "7 天", 3: "3 天", 1: "1 天"}
# 代理提醒的补充说明（P4-05 明确要求）
AGENT_HINT = "到期后将释放占用额度，续费需重新占用，请预留额度"
MEMBER_HINT = "到期后功能会全部停止，请联系你的上级续费"


def stage_code(days: int) -> str:
    """阶段编码：3 → '3d'。"""
    return f"{int(days)}d"


def stage_label(stage: str) -> str:
    """阶段编码转显示名：'3d' → '3 天'。"""
    try:
        return STAGE_LABELS.get(int(str(stage).rstrip("d")), str(stage))
    except (TypeError, ValueError):
        return str(stage)


def due_stages(days_left: int | None, *, audience: str) -> list[int]:
    """某个剩余天数下，该受众还没到过的提醒阶段（从宽到紧）。"""
    if days_left is None or days_left < 0:
        return []
    stages = AGENT_STAGES if audience == AUDIENCE_AGENT else MEMBER_STAGES
    return [stage for stage in stages if days_left <= stage]


async def _existing(
    session: AsyncSession,
    tenant_ids: list[int],
) -> set[tuple[int, str, str]]:
    if not tenant_ids:
        return set()
    rows = await session.scalars(
        select(TenantReminder).where(TenantReminder.tenant_id.in_(tenant_ids))
    )
    return {(row.tenant_id, row.stage, row.audience) for row in rows}


def format_message(
    *,
    audience: str,
    display_name: str,
    stage: str,
    expires_at: datetime | None,
    tz_name: str | None = None,
) -> str:
    """提醒文案（TG 通知用）。"""
    label = stage_label(stage)
    deadline = ""
    if expires_at is not None:
        moment = as_utc(expires_at).astimezone(resolve_timezone(tz_name))
        deadline = f"，到期时间 {moment:%Y-%m-%d %H:%M:%S}"
    if audience == AUDIENCE_AGENT:
        return (
            f"【额度提醒】你名下的会员「{display_name}」还有 {label} 到期{deadline}。{AGENT_HINT}。"
        )
    return f"【到期提醒】你的账号「{display_name}」还有 {label} 到期{deadline}。{MEMBER_HINT}。"


async def _notify_bots(session: AsyncSession, tenant_ids: list[int]) -> dict[int, ControlBot]:
    """租户 → 该租户启用的默认通知 Bot（取不到就由调用方退回自营）。"""
    wanted = sorted(set(tenant_ids) | {SELF_TENANT_ID})
    rows = list(
        await session.scalars(
            select(ControlBot)
            .where(
                ControlBot.tenant_id.in_(wanted),
                ControlBot.is_default.is_(True),
                ControlBot.enabled.is_(True),
            )
            .order_by(ControlBot.id)
        )
    )
    resolved: dict[int, ControlBot] = {}
    for bot in rows:
        resolved.setdefault(bot.tenant_id, bot)
    return resolved


async def _display_names(session: AsyncSession, tenant_ids: list[int]) -> dict[int, str]:
    """租户 → 展示名（优先会员用户名，取不到用租户名）。"""
    if not tenant_ids:
        return {}
    tenants = list(await session.scalars(select(Tenant).where(Tenant.id.in_(tenant_ids))))
    owner_ids = [item.owner_user_id for item in tenants if item.owner_user_id]
    usernames: dict[int, str] = {}
    if owner_ids:
        rows = await session.execute(select(User.id, User.username).where(User.id.in_(owner_ids)))
        usernames = {int(row[0]): str(row[1]) for row in rows}
    return {item.id: usernames.get(item.owner_user_id or 0) or item.name for item in tenants}


async def _default_sender(_config: AppConfig, token: str) -> Any:
    """真实通道：Bot API（只有真发消息时才联网）。"""
    return BotApiClient(token)


async def deliver(
    session: AsyncSession,
    reminders: list[TenantReminder],
    *,
    config: AppConfig,
    sender_factory: Any = None,
) -> dict[str, Any]:
    """把站内提醒补一条 TG 通知（P4-05 的第 2 个渠道）。

    出口取「租户自己的默认通知 Bot」，没配就退回自营租户的默认 Bot；收件人是
    Bot 里的「管理员 TG 用户 ID」。没 Bot / 没管理员 / 发送失败都只留在站内渠道，
    单条失败不影响其他提醒，也不影响到期强停。
    """
    pending = [row for row in reminders if row.channel == CHANNEL_INAPP]
    outcome: dict[str, Any] = {"sent": 0, "failed": 0, "skipped": 0, "messages": 0}
    if not pending:
        return outcome
    if config.app.demo_mode:
        # 本地演练：不真发 TG（Bot API 会联网），提醒留在站内
        outcome["skipped"] = len(pending)
        return outcome

    tenant_ids = [row.tenant_id for row in pending]
    bots = await _notify_bots(session, tenant_ids)
    names = await _display_names(session, tenant_ids)
    factory = sender_factory or _default_sender
    clients: dict[int, Any] = {}
    try:
        for row in pending:
            bot = bots.get(row.tenant_id) or bots.get(SELF_TENANT_ID)
            targets = bot_service.load_admin_ids(bot) if bot is not None else []
            if bot is None or not targets:
                outcome["skipped"] += 1
                continue
            client = clients.get(bot.id)
            if client is None:
                try:
                    client = await factory(config, bot_service.decrypt_token(config, bot))
                except Exception as exc:  # noqa: BLE001 - 单个 Bot 不可用不该拖垮整批
                    logger.warning("到期提醒：通知 Bot「{}」不可用：{}", bot.name, exc)
                    outcome["skipped"] += 1
                    continue
                clients[bot.id] = client
            text = format_message(
                audience=row.audience,
                display_name=names.get(row.tenant_id) or f"#{row.tenant_id}",
                stage=row.stage,
                expires_at=row.expires_at,
                tz_name=config.app.timezone,
            )
            sent = False
            for chat_id in targets:
                try:
                    await client.send_message(str(chat_id), text)
                except Exception as exc:  # noqa: BLE001 - 单个收件人失败不影响其他
                    logger.warning(
                        "到期提醒发送失败（租户 #{} → {}）：{}",
                        row.tenant_id,
                        chat_id,
                        exc,
                    )
                    continue
                sent = True
                outcome["messages"] += 1
            if sent:
                row.channel = CHANNEL_TELEGRAM
                outcome["sent"] += 1
            else:
                outcome["failed"] += 1
        if outcome["sent"]:
            await session.commit()
    finally:
        for client in clients.values():
            with contextlib.suppress(Exception):
                await client.close()
    return outcome


async def sweep(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    tz_name: str | None = None,
) -> dict[str, Any]:
    """扫描全部会员租户，为进入窗口的阶段补提醒（幂等）。"""
    moment = as_utc(now) or datetime.now(UTC)
    tenants = list(
        await session.scalars(
            select(Tenant).where(Tenant.kind == TENANT_KIND_MEMBER).order_by(Tenant.id)
        )
    )
    existing = await _existing(session, [tenant.id for tenant in tenants])

    created: list[TenantReminder] = []
    for tenant in tenants:
        if tenant.expires_at is None:
            continue
        state = tenant_status_service.evaluate(tenant, now=moment, tz_name=tz_name)
        # 停用中的不催；已过期的也不再生成（强停已经发生）
        if state.status != TENANT_STATUS_ACTIVE:
            continue
        for audience in (AUDIENCE_AGENT, AUDIENCE_MEMBER):
            if audience == AUDIENCE_AGENT and tenant.owner_agent_id is None:
                continue  # 平台直开的号没有代理可提醒
            for stage in due_stages(state.days_left, audience=audience):
                key = (tenant.id, stage_code(stage), audience)
                if key in existing:
                    continue
                reminder = TenantReminder(
                    tenant_id=tenant.id,
                    stage=stage_code(stage),
                    audience=audience,
                    channel=CHANNEL_INAPP,
                    days_left=state.days_left,
                    expires_at=state.expires_at,
                    sent_at=moment,
                    note=AGENT_HINT if audience == AUDIENCE_AGENT else MEMBER_HINT,
                )
                session.add(reminder)
                existing.add(key)
                created.append(reminder)

    if created:
        await session.flush()
        await session.commit()
    return {
        "created": [
            {
                "tenant_id": row.tenant_id,
                "stage": row.stage,
                "audience": row.audience,
                "days_left": row.days_left,
            }
            for row in created
        ],
        "count": len(created),
        # 内部用：TG 渠道直接拿这批记录去发，不用再查一次库
        "rows": created,
        "generated_at": moment,
    }


async def stages_by_tenant(
    session: AsyncSession,
    tenant_ids: list[int],
) -> dict[int, list[str]]:
    """租户 → 已提醒的阶段编码（给后台列表显示"已提醒"）。"""
    if not tenant_ids:
        return {}
    rows = await session.scalars(
        select(TenantReminder)
        .where(TenantReminder.tenant_id.in_(tenant_ids))
        .order_by(TenantReminder.id)
    )
    result: dict[int, list[str]] = {}
    for row in rows:
        codes = result.setdefault(row.tenant_id, [])
        if row.stage not in codes:
            codes.append(row.stage)
    return result


async def list_reminders(
    session: AsyncSession,
    *,
    tenant_ids: list[int] | None = None,
    audience: str | None = None,
    limit: int = 100,
) -> list[TenantReminder]:
    """最近的提醒记录（平台后台用；``tenant_ids`` 为空表示不按租户过滤）。"""
    statement = select(TenantReminder).order_by(TenantReminder.id.desc())
    if tenant_ids is not None:
        statement = statement.where(TenantReminder.tenant_id.in_(tenant_ids or [-1]))
    if audience:
        statement = statement.where(TenantReminder.audience == audience)
    return list(await session.scalars(statement.limit(limit)))
