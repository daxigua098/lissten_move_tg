"""在线补搜：搜索时的按需补搜（F-R19）。

资源发现的渠道收敛成两条：**按需补搜**（这里）与**目录同步**
（``directory_sync_service``）。「预置关键词 / 句式 / 热门词任务」那套已经下线，
不再有无人值守的关键词搜索。

三条硬规则：

1. **失败隔离**：某一路径出错只记在它自己那一条上，不影响其它路径；
2. **配额**：Telegram 搜索按关键词计入 ``searches``；
3. **限速**：命中 FLOOD_WAIT 只提示需要等待多久，不做重试轰炸。

combot 没有关键词接口（实测只有榜单与语言榜），所以它的"补搜"是
**在本地已同步的目录里按词匹配**，不回三方站出网——结果里标 ``status=local``。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.errors import ValidationFailedError
from app.core.telegram_client import ChatProfile, search_public_chats
from app.db.models import DISCOVER_KEYWORD
from app.services import resource_quota_service, resource_service
from app.services.resource_service import ResourceRef

# 在线补搜支持的渠道
ONLINE_SITES = ("telegram", "combot", "tgme")
# 目录站渠道（受 ``directory_enabled`` 开关控制）
DIRECTORY_ONLINE_SITES = ("combot", "tgme")

FLOOD_WAIT_PATTERN = re.compile(r"flood[_ ]?wait[^\d]*(\d+)", re.IGNORECASE)
WAIT_OF_PATTERN = re.compile(r"a wait of (\d+) seconds", re.IGNORECASE)
# 知道是限流但拿不到秒数时的默认提示时长
DEFAULT_FLOOD_WAIT_SECONDS = 300


def parse_flood_wait(exc: BaseException | str) -> int:
    """从异常里取 FLOOD_WAIT 的秒数；不是限流就返回 0。

    Telethon 的 ``FloodWaitError`` 有 ``seconds`` 属性；文本兜底要同时认
    ``FLOOD_WAIT_3600`` 和 ``A wait of 3600 seconds`` 两种官方措辞。
    """
    seconds = getattr(exc, "seconds", None)
    if isinstance(seconds, int) and seconds > 0:
        return seconds
    text = str(exc)
    for pattern in (FLOOD_WAIT_PATTERN, WAIT_OF_PATTERN):
        match = pattern.search(text)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                break
    if "flood" in text.lower():
        return DEFAULT_FLOOD_WAIT_SECONDS
    return 0


async def search_online(
    session: AsyncSession,
    config: AppConfig,
    client: Any,
    *,
    keywords: list[str],
    sites: Sequence[str] = ONLINE_SITES,
    limit: int | None = None,
    account_id: int | None = None,
    directory_fetcher: Any = None,
) -> dict[str, Any]:
    """搜索时的「在线补搜」：本地结果已经先渲染了，这一步只是追加。"""
    cleaned = [item.strip() for item in keywords if item and item.strip()][:10]
    if not cleaned:
        raise ValidationFailedError("在线补搜至少要有一个关键词")

    section = config.resource
    effective_limit = int(limit or section.search_online_limit)
    allowed = set(ONLINE_SITES)
    if not section.directory_enabled:
        allowed -= set(DIRECTORY_ONLINE_SITES)
    chosen = [item for item in sites if item in allowed]
    if not chosen:
        raise ValidationFailedError(f"补搜渠道必须是 {'/'.join(ONLINE_SITES)} 之一")

    results: list[dict[str, Any]] = []
    for site in chosen:
        if site == "telegram":
            results.append(
                await _search_online_telegram(
                    session,
                    client,
                    cleaned,
                    limit=effective_limit,
                    account_id=account_id,
                )
            )
        elif site == "combot":
            results.append(await _search_online_combot(session, cleaned))
        else:
            results.append(
                await _search_online_tgme(
                    session,
                    cleaned,
                    limit=effective_limit,
                    fetcher=directory_fetcher,
                )
            )

    return {
        "keywords": cleaned,
        "results": results,
        "new_total": sum(int(item.get("new_resources") or 0) for item in results),
    }


async def _search_online_telegram(
    session: AsyncSession,
    client: Any,
    keywords: list[str],
    *,
    limit: int,
    account_id: int | None,
) -> dict[str, Any]:
    """Telegram 侧：按词搜公开群。"""
    hits = 0
    created = 0
    try:
        for keyword in keywords:
            found, added = await _search_keyword(session, client, keyword, limit=limit)
            hits += found
            created += added
            await resource_quota_service.bump(session, account_id, searches=1)
    except Exception as exc:  # noqa: BLE001 - 单渠道失败不影响其它渠道
        logger.warning("在线补搜（Telegram）失败：{}", exc)
        wait_seconds = parse_flood_wait(exc)
        if wait_seconds:
            await resource_quota_service.bump(session, account_id, flood_waits=1)
            message = f"Telegram 限流，需要等待约 {wait_seconds} 秒后再试"
        else:
            message = str(exc)[:200]
        return {
            "site": "telegram",
            "status": "error",
            "hits": hits,
            "new_resources": created,
            "error": message,
        }
    return {
        "site": "telegram",
        "status": "ok",
        "hits": hits,
        "new_resources": created,
        "error": None,
    }


async def _search_online_combot(
    session: AsyncSession,
    keywords: list[str],
) -> dict[str, Any]:
    """combot 侧：只在本地已同步的目录里按词匹配（不出网）。"""
    hits = 0
    for keyword in keywords:
        _rows, total = await resource_service.list_resources(
            session,
            keyword=keyword,
            source_site="combot",
            limit=1,
            offset=0,
        )
        hits += total
    return {
        "site": "combot",
        "status": "local",
        "hits": hits,
        "new_resources": 0,
        "error": None,
    }


async def _search_online_tgme(
    session: AsyncSession,
    keywords: list[str],
    *,
    limit: int,
    fetcher: Any,
) -> dict[str, Any]:
    """tg-me 侧：抓每个词的第 1 页列表。"""
    from app.services import directory_sync_service

    if fetcher is None:
        return {
            "site": "tgme",
            "status": "error",
            "hits": 0,
            "new_resources": 0,
            "error": "没有可用的抓取器",
        }
    hits = 0
    created = 0
    try:
        for keyword in keywords:
            entries = await directory_sync_service.fetch_page(
                fetcher,
                "tgme",
                keyword,
                1,
            )
            entries = entries[: max(1, limit)]
            hits += len(entries)
            created += await directory_sync_service.store_entries(
                session,
                entries,
                source="tgme",
                scope=keyword,
                page=1,
            )
    except Exception as exc:  # noqa: BLE001 - 单渠道失败不影响其它渠道
        logger.warning("在线补搜（tg-me）失败：{}", exc)
        return {
            "site": "tgme",
            "status": "error",
            "hits": hits,
            "new_resources": created,
            "error": str(exc)[:200],
        }
    return {
        "site": "tgme",
        "status": "ok",
        "hits": hits,
        "new_resources": created,
        "error": None,
    }


async def _search_keyword(
    session: AsyncSession,
    client: Any,
    keyword: str,
    *,
    limit: int,
) -> tuple[int, int]:
    """按关键词搜一轮公开群，结果写进候选池。"""
    profiles = await search_public_chats(client, keyword, limit=limit)
    created = await _store_profiles(
        session,
        profiles,
        discovered_by=DISCOVER_KEYWORD,
        discovered_from=f"在线补搜：{keyword}",
    )
    return len(profiles), created


async def _store_profiles(
    session: AsyncSession,
    profiles: list[ChatProfile],
    *,
    discovered_by: str,
    discovered_from: str,
) -> int:
    """把搜索结果写进候选池，返回新增条数。"""
    created = 0
    for profile in profiles:
        if not profile.tg_id:
            continue
        outcome = await resource_service.upsert_resource(
            session,
            ResourceRef(
                tg_id=int(profile.tg_id),
                title=profile.title or "",
                username=profile.username,
                chat_type=profile.chat_type,
                member_count=profile.member_count,
                member_count_approx=bool(profile.member_count and profile.member_count >= 5000),
            ),
            discovered_by=discovered_by,
            discovered_from=discovered_from,
        )
        if outcome.created:
            created += 1
    return created
