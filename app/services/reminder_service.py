"""到期提醒（P4-05）：按阶段生成去重提醒，并汇总给后台列表展示。

规则（八项决议第 7 条 + P4-05）：

- **代理**：到期前 7 / 3 / 1 天各提醒一次，文案带"请预留额度"；
- **会员**：到期前 3 / 1 天各提醒一次（登录后横幅 + 后台到期列表）；
- 停用中的租户不再催（停用是处罚，催续费没有意义）；已过期也不再生成；
- 续期后剩余天数回到窗口外，提醒自然从列表消失，历史记录保留可追溯。

渠道：目前只实现站内（``inapp``）。要接 TG / 邮件 / 短信，只需要在
:func:`deliver` 里补一个发送实现，去重表与调用方都不用改。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import as_utc
from app.db.models import (
    AUDIENCE_AGENT,
    AUDIENCE_MEMBER,
    CHANNEL_INAPP,
    TENANT_KIND_MEMBER,
    TENANT_STATUS_ACTIVE,
    Tenant,
    TenantReminder,
)
from app.services import tenant_status_service

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


async def deliver(reminders: list[TenantReminder]) -> list[TenantReminder]:
    """把提醒发出去。当前只有站内渠道，所以这里只负责记录。

    返回真正"发出"的提醒；接 TG / 邮件时在这里分流，其余调用方不用改。
    """
    return [row for row in reminders if row.channel == CHANNEL_INAPP]


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
