"""资源探测：把一次 Telegram 读取变成一组指标，并顺手滚雪球（F-R03 / F-R06）。

单条资源的探测请求数控制在 2～4 次：

1. ``resolve`` 拿实体（本地缓存命中时不额外请求）；
2. ``getFullChannel`` 拿简介与成员数；
3. ``getHistory`` 采样最近 N 条消息；
4. 需要时再解析一次待回填的 tg_id。

失败也要落一条日志（``result=failed``），否则"为什么这个群一直没数据"
在界面上就无从判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.link_extractor import extract_links
from app.core.resource_probe import ProbeThresholds, compute_metrics
from app.core.source_resolver import ResolvedTarget
from app.core.telegram_client import (
    fetch_chat_full,
    sample_chat_messages,
)
from app.core.telegram_client import (
    fetch_chat_profile as _fetch_profile,
)
from app.db.base import utc_now
from app.db.models import (
    PROBE_FAILED,
    PROBE_OK,
    STATE_ACTIVE,
    STATE_BANNED,
    STATE_LEFT,
    STATE_PRIVATE,
    TgResource,
)
from app.services import keyword_service, resource_quota_service, resource_service
from app.services.resource_service import ResourceRef

# 来自索引型群的链接排到普通候选前面：探测队列按 next_refresh_at 升序，
# 这里把它们的排队时间往前挪一点，保证先被探测（F-R03 的优先级）。
PRIORITY_OFFSET = timedelta(minutes=5)


@dataclass(frozen=True)
class ProbeOutcome:
    """一次探测的结果。"""

    resource: TgResource
    result: str
    error: str | None = None
    requests_used: int = 0
    new_resources: int = 0
    links_found: int = 0


def thresholds_from_config(config: AppConfig) -> ProbeThresholds:
    """把配置里的阈值翻成探测口径。"""
    section = config.resource
    return ProbeThresholds(
        index_member_threshold=section.index_member_threshold,
        index_feature_threshold=section.index_feature_threshold,
        activity_threshold=section.activity_threshold,
        link_density_threshold=section.link_density_threshold,
    )


async def absorb_links(
    session: AsyncSession,
    config: AppConfig,
    *,
    text: str | None,
    source_title: str,
    discovered_by: str = "link",
    priority: bool = False,
) -> int:
    """从一段文本里提链接进候选池（返回新增条数）。

    三处调用：群简介 / 置顶消息（探测时）、采样消息正文（探测时）、
    监听中的实时消息（运行时）。``priority=True`` 用在"来源是索引型群"的场景。
    """
    hits = extract_links(text)
    if not hits:
        return 0

    now = utc_now()
    queued_at = now - PRIORITY_OFFSET if enabled_priority(config, priority) else now
    created = 0
    for hit in hits:
        ref = (
            ResourceRef(title=hit.value, username=hit.value, source_url=hit.raw)
            if hit.kind == "username"
            else ResourceRef(
                title=f"邀请链接 {hit.value[:8]}",
                invite_link=f"https://t.me/+{hit.value}",
                source_url=hit.raw,
            )
        )
        outcome = await resource_service.upsert_resource(
            session,
            ref,
            discovered_by=discovered_by,
            discovered_from=source_title or None,
        )
        if outcome.resource is None:
            continue
        if outcome.created:
            created += 1
            outcome.resource.next_refresh_at = queued_at
        elif outcome.resource.last_probed_at is None and enabled_priority(config, priority):
            # 还没探过又来自索引型群：插到队首
            outcome.resource.next_refresh_at = queued_at
    if created:
        await session.commit()
    return created


def enabled_priority(config: AppConfig, requested: bool) -> bool:
    """索引型群的链接优先级是否生效。"""
    return bool(requested and config.resource.index_source_priority)


async def probe_resource(
    session: AsyncSession,
    config: AppConfig,
    client: Any,
    resource: TgResource,
    *,
    account_id: int | None = None,
    sample_depth: int | None = None,
    snowball: bool = True,
) -> ProbeOutcome:
    """对一条资源执行一次探测。"""
    resource_service.ensure_probeable(resource)
    depth = int(sample_depth or config.resource.sample_depth)
    requests_used = 0

    tg_id = resource.tg_id
    if tg_id is None:
        ref, resolve_requests = await _resolve_ref(client, resource)
        requests_used += resolve_requests
        if ref is None:
            return await _fail(
                session,
                config,
                resource,
                error="无法解析该资源：可能已失效，或该账号没有访问权限",
                state=STATE_PRIVATE,
                account_id=account_id,
                requests_used=requests_used,
            )
        tg_id = ref.tg_id
        if not tg_id:
            return await _fail(
                session,
                config,
                resource,
                error="该资源需要先加入才能读取（私密群/频道）",
                state=STATE_PRIVATE,
                account_id=account_id,
                requests_used=requests_used,
            )
        ref_outcome = await resource_service.upsert_resource(
            session,
            ResourceRef(
                tg_id=tg_id,
                title=resource.title,
                username=resource.username or ref.username,
                invite_link=resource.invite_link,
                chat_type=ref.chat_type,
                member_count=ref.member_count,
            ),
            blacklist_check=False,
        )
        resource = ref_outcome.resource or resource

    try:
        full = await fetch_chat_full(client, tg_id)
        requests_used += 1
    except Exception as exc:  # noqa: BLE001 - 读取失败按探测失败记录
        state = _state_from_error(exc)
        return await _fail(
            session,
            config,
            resource,
            error=_short_error(exc),
            state=state,
            account_id=account_id,
            requests_used=requests_used,
        )

    try:
        samples = await sample_chat_messages(client, tg_id, limit=depth)
        requests_used += 1
    except Exception as exc:  # noqa: BLE001 - 读不到消息仍然用资料算一部分指标
        logger.info("资源 {} 采样消息失败（继续用资料推断）：{}", tg_id, exc)
        samples = []

    entries = await keyword_service.load_entries(session, None)
    metrics = compute_metrics(
        samples,
        title=resource.title,
        about=full.about,
        member_count=full.member_count,
        keyword_entries=entries,
        thresholds=thresholds_from_config(config),
    )

    new_resources = 0
    links_found = 0
    if snowball:
        texts = [full.about or ""] + [str(getattr(item, "text", "") or "") for item in samples]
        links_found = sum(len(extract_links(text)) for text in texts)
        source_title = resource_service.serialize_resource(resource)["name"]
        for text in texts:
            new_resources += await absorb_links(
                session,
                config,
                text=text,
                source_title=source_title,
                priority=bool(resource.is_index_group or metrics.is_index_group),
            )

    updated = await resource_service.apply_metrics(
        session,
        resource,
        metrics,
        account_id=account_id,
        result=PROBE_OK,
        requests_used=requests_used,
        about=full.about,
        member_count=full.member_count,
        member_count_approx=full.member_count_approx,
    )
    # 探测成功说明这个资源现在读得到
    updated.resource_state = STATE_ACTIVE
    updated.next_refresh_at = resource_service.next_refresh_at(updated, config)
    await session.commit()
    await session.refresh(updated)

    await resource_quota_service.bump(session, account_id, probes=1)
    return ProbeOutcome(
        resource=updated,
        result=PROBE_OK,
        requests_used=requests_used,
        new_resources=new_resources,
        links_found=links_found,
    )


async def probe_many(
    session: AsyncSession,
    config: AppConfig,
    client: Any,
    resources: list[TgResource],
    *,
    account_id: int | None = None,
    sample_depth: int | None = None,
) -> dict[str, Any]:
    """批量探测（F-R10 的"批量刷新"），返回成功 / 失败清单。"""
    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    new_resources = 0
    for resource in resources:
        fresh = await resource_service.get_resource(session, resource.id)
        if fresh is None:
            continue
        outcome = await probe_resource(
            session,
            config,
            client,
            fresh,
            account_id=account_id,
            sample_depth=sample_depth,
        )
        item = {
            "id": fresh.id,
            "title": fresh.title,
            "tg_id": fresh.tg_id,
            "result": outcome.result,
        }
        if outcome.result == PROBE_OK:
            succeeded.append(item)
            new_resources += outcome.new_resources
        else:
            failed.append({**item, "error": outcome.error})
    return {
        "succeeded": succeeded,
        "failed": failed,
        "new_resources": new_resources,
    }


async def _resolve_ref(client: Any, resource: TgResource) -> tuple[Any, int]:
    """把只有 username / 邀请链接的候选解析成带 tg_id 的资料。"""
    target: ResolvedTarget | None = None
    if resource.username:
        target = ResolvedTarget(
            kind="username",
            value=resource.username.lstrip("@"),
            raw_input=resource.username,
        )
    elif resource.invite_link:
        invite_hash = resource.invite_link.rstrip("/").rsplit("/", 1)[-1].lstrip("+")
        if invite_hash:
            target = ResolvedTarget(
                kind="invite",
                value=invite_hash,
                raw_input=resource.invite_link,
            )
    if target is None:
        return None, 0
    try:
        return await _fetch_profile(client, target), 1
    except Exception as exc:  # noqa: BLE001 - 解析失败由调用方转成探测失败
        logger.info("解析资源 {} 失败：{}", resource.id, exc)
        return None, 1


async def _fail(
    session: AsyncSession,
    config: AppConfig,
    resource: TgResource,
    *,
    error: str,
    state: str | None,
    account_id: int | None,
    requests_used: int,
) -> ProbeOutcome:
    """记一条失败的探测日志，并保留上一次的指标。"""
    from app.core.resource_probe import ProbeMetrics

    updated = await resource_service.apply_metrics(
        session,
        resource,
        ProbeMetrics(),
        account_id=account_id,
        result=PROBE_FAILED,
        error=error,
        requests_used=requests_used,
    )
    if state:
        updated.resource_state = state
    # 失败也要排下次刷新，否则这条资源会一直挂在队首反复重试
    updated.next_refresh_at = utc_now() + timedelta(days=config.resource.refresh_low_days)
    await session.commit()
    await session.refresh(updated)
    return ProbeOutcome(
        resource=updated,
        result=PROBE_FAILED,
        error=error,
        requests_used=requests_used,
    )


def _state_from_error(exc: BaseException) -> str | None:
    """把读群失败的报错收敛成资源状态（active/private/banned/left）。"""
    text = str(exc).lower()
    if "banned" in text or "kicked" in text or "deactivated" in text:
        return STATE_BANNED
    if "you are not a participant" in text or "not a member" in text:
        return STATE_LEFT
    if "private" in text or "no access" in text or "chat_write_forbidden" in text:
        return STATE_PRIVATE
    return None


def _short_error(exc: BaseException) -> str:
    return (str(exc) or exc.__class__.__name__)[:200]
