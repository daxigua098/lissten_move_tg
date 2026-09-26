"""发现器：关键词搜索 / 句式搜索 / 热门词驱动（F-R02 / F-R04 / F-R05）。

所有 Telegram 请求都发生在"跑任务"这一刻，页面读取只查本地库。
限流与配额的规矩：

- 命中 FLOOD_WAIT 不算失败，自动排队到等待结束之后，并记一次 ``flood_waits``；
- 同一个关键词在 ``search_repeat_hours`` 内不重复搜；
- 当天搜索额度用完就排到第二天，而不是报错（需求书 8.2）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.telegram_client import (
    ChatProfile,
    search_global_messages,
    search_public_chats,
)
from app.db.base import utc_now
from app.db.models import (
    DISCOVER_HOTWORD,
    DISCOVER_KEYWORD,
    DISCOVER_LINK,
    DISCOVER_PHRASE,
    DISCOVER_SOURCES,
    HotKeyword,
    ResourceDiscoverTask,
)
from app.services import resource_quota_service, resource_service
from app.services.resource_service import ResourceRef

# F-R05 的句式模板：用户真正在群里喊的那些话
PHRASE_PRESETS: tuple[str, ...] = (
    "求群",
    "求拉",
    "拉我进群",
    "资源在哪",
    "进群",
    "有没有群",
    "求资源群",
    "群链接",
)

FLOOD_WAIT_PATTERN = re.compile(r"flood[_ ]?wait[^\d]*(\d+)", re.IGNORECASE)
WAIT_OF_PATTERN = re.compile(r"a wait of (\d+) seconds", re.IGNORECASE)
# 知道是限流但拿不到秒数时的默认排队时长
DEFAULT_FLOOD_WAIT_SECONDS = 300


@dataclass(frozen=True)
class DiscoverOutcome:
    """一次发现任务的结果。"""

    task_id: int | None
    keyword: str
    hits: int = 0
    new_resources: int = 0
    skipped: str | None = None
    error: str | None = None
    wait_seconds: int = 0


# ------------------------------------------------------------------ 任务管理


async def list_tasks(
    session: AsyncSession,
    *,
    kind: str | None = None,
    enabled: bool | None = None,
) -> list[ResourceDiscoverTask]:
    """发现任务列表。"""
    statement = select(ResourceDiscoverTask).order_by(ResourceDiscoverTask.id)
    if kind:
        statement = statement.where(ResourceDiscoverTask.kind == kind)
    if enabled is not None:
        statement = statement.where(ResourceDiscoverTask.enabled.is_(enabled))
    return list(await session.scalars(statement))


async def get_task(session: AsyncSession, task_id: int) -> ResourceDiscoverTask | None:
    """按 ID 查任务。"""
    return await session.get(ResourceDiscoverTask, task_id)


async def create_task(
    session: AsyncSession,
    *,
    kind: str,
    keyword: str,
    category: str | None = None,
    enabled: bool = True,
) -> ResourceDiscoverTask:
    """新增一个发现关键词 / 句式。同一种类里同词不重复。"""
    if kind not in DISCOVER_SOURCES:
        raise ValidationFailedError(f"任务类型必须是 {'/'.join(DISCOVER_SOURCES)} 之一")
    clean = (keyword or "").strip()
    if not clean:
        raise ValidationFailedError("关键词不能为空")
    existing = await session.scalar(
        select(ResourceDiscoverTask).where(
            ResourceDiscoverTask.kind == kind,
            ResourceDiscoverTask.keyword == clean,
        )
    )
    if existing is not None:
        raise ConflictError(f"「{clean}」已经在发现任务里了")
    task = ResourceDiscoverTask(
        kind=kind,
        keyword=clean[:64],
        category=(category or "").strip() or None,
        enabled=enabled,
        next_run_at=utc_now(),
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def update_task(
    session: AsyncSession,
    task_id: int,
    *,
    keyword: str | None = None,
    category: str | None = None,
    enabled: bool | None = None,
) -> ResourceDiscoverTask:
    """改关键词 / 分类 / 启停。"""
    task = await session.get(ResourceDiscoverTask, task_id)
    if task is None:
        raise NotFoundError("发现任务不存在")
    if keyword is not None:
        clean = keyword.strip()
        if not clean:
            raise ValidationFailedError("关键词不能为空")
        task.keyword = clean[:64]
    if category is not None:
        task.category = category.strip() or None
    if enabled is not None:
        task.enabled = enabled
        if enabled and task.next_run_at is None:
            task.next_run_at = utc_now()
    await session.commit()
    await session.refresh(task)
    return task


async def delete_task(session: AsyncSession, task_id: int) -> None:
    """删除任务。"""
    task = await session.get(ResourceDiscoverTask, task_id)
    if task is None:
        raise NotFoundError("发现任务不存在")
    await session.delete(task)
    await session.commit()


async def import_hotwords(
    session: AsyncSession,
    *,
    top_n: int = 20,
    min_count: int = 2,
) -> dict[str, Any]:
    """F-R04：把「热门关键词」Top N 一键加进发现关键词库。"""
    rows = list(
        await session.scalars(
            select(HotKeyword)
            .where(HotKeyword.count >= max(1, min_count))
            .order_by(HotKeyword.count.desc())
            .limit(max(1, top_n))
        )
    )
    added: list[str] = []
    skipped: list[str] = []
    for row in rows:
        existing = await session.scalar(
            select(ResourceDiscoverTask).where(
                ResourceDiscoverTask.kind == DISCOVER_HOTWORD,
                ResourceDiscoverTask.keyword == row.token,
            )
        )
        if existing is not None:
            skipped.append(row.token)
            continue
        session.add(
            ResourceDiscoverTask(
                kind=DISCOVER_HOTWORD,
                keyword=row.token[:64],
                category="热门词",
                enabled=True,
                next_run_at=utc_now(),
            )
        )
        added.append(row.token)
    if added:
        await session.commit()
    return {"added": added, "skipped": skipped, "total": len(rows)}


async def ensure_phrase_presets(session: AsyncSession) -> int:
    """F-R05：写入一批句式模板（已存在的跳过）。"""
    created = 0
    for phrase in PHRASE_PRESETS:
        existing = await session.scalar(
            select(ResourceDiscoverTask).where(
                ResourceDiscoverTask.kind == DISCOVER_PHRASE,
                ResourceDiscoverTask.keyword == phrase,
            )
        )
        if existing is not None:
            continue
        session.add(
            ResourceDiscoverTask(
                kind=DISCOVER_PHRASE,
                keyword=phrase,
                category="句式",
                enabled=True,
                next_run_at=utc_now(),
            )
        )
        created += 1
    if created:
        await session.commit()
    return created


async def next_due_task(session: AsyncSession) -> ResourceDiscoverTask | None:
    """取一个到点的启用任务（运行时循环用）。"""
    moment = utc_now()
    return await session.scalar(
        select(ResourceDiscoverTask)
        .where(
            ResourceDiscoverTask.enabled.is_(True),
            ResourceDiscoverTask.kind != DISCOVER_LINK,
        )
        .where(
            (ResourceDiscoverTask.next_run_at.is_(None))
            | (ResourceDiscoverTask.next_run_at <= moment)
        )
        .order_by(ResourceDiscoverTask.next_run_at.asc().nulls_first())
    )


# ------------------------------------------------------------------ 执行


async def run_task(
    session: AsyncSession,
    config: AppConfig,
    client: Any,
    task: ResourceDiscoverTask,
    *,
    limit: int = 20,
    account_id: int | None = None,
) -> DiscoverOutcome:
    """执行一个发现任务。"""
    section = config.resource
    now = utc_now()

    if not task.enabled:
        return DiscoverOutcome(task.id, task.keyword, skipped="disabled")

    # 同一个关键词在窗口期内不重复搜（F-R02）
    if task.last_run_at is not None:
        last_run = task.last_run_at
        if last_run.tzinfo is None:
            # SQLite 读回来是 naive，补上 UTC 再比
            last_run = last_run.replace(tzinfo=now.tzinfo)
        elapsed = now - last_run
        if elapsed < timedelta(hours=section.search_repeat_hours):
            task.next_run_at = last_run + timedelta(hours=section.search_repeat_hours)
            await session.commit()
            return DiscoverOutcome(task.id, task.keyword, skipped="recent")

    quota = await resource_quota_service.get_quota(session, account_id)
    used = int(quota.searches) if quota else 0
    if account_id is not None and used >= section.search_daily_limit:
        # 超限排队到第二天，不报错
        task.next_run_at = (now + timedelta(days=1)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        await session.commit()
        return DiscoverOutcome(task.id, task.keyword, skipped="quota")

    task.last_run_at = now
    try:
        if task.kind == DISCOVER_PHRASE:
            hits, created = await _run_phrase(session, config, client, task, limit=limit)
        else:
            hits, created = await _run_keyword(session, config, client, task, limit=limit)
    except Exception as exc:  # noqa: BLE001 - 单个任务失败不影响其他任务
        wait_seconds = parse_flood_wait(exc)
        task.last_error = str(exc)[:200]
        if wait_seconds:
            task.flood_waits += 1
            task.next_run_at = now + timedelta(seconds=wait_seconds)
            await resource_quota_service.bump(session, account_id, flood_waits=1)
        else:
            task.next_run_at = now + timedelta(hours=1)
        await session.commit()
        logger.warning("发现任务「{}」失败：{}", task.keyword, exc)
        return DiscoverOutcome(
            task.id,
            task.keyword,
            skipped="flood_wait" if wait_seconds else "error",
            error=str(exc)[:200],
            wait_seconds=wait_seconds,
        )

    task.hits += hits
    task.new_found += created
    task.last_error = None
    task.next_run_at = now + timedelta(hours=section.search_repeat_hours)
    await session.commit()
    await session.refresh(task)
    await resource_quota_service.bump(session, account_id, searches=1)
    return DiscoverOutcome(
        task.id,
        task.keyword,
        hits=hits,
        new_resources=created,
    )


async def _run_keyword(
    session: AsyncSession,
    config: AppConfig,
    client: Any,
    task: ResourceDiscoverTask,
    *,
    limit: int,
) -> tuple[int, int]:
    """关键词（含热门词）搜索：contacts.Search → 候选池。"""
    profiles: list[ChatProfile] = await search_public_chats(client, task.keyword, limit=limit)
    created = await _store_profiles(
        session,
        profiles,
        discovered_by=task.kind,
        discovered_from=f"搜索词：{task.keyword}",
    )
    return len(profiles), created


async def _run_phrase(
    session: AsyncSession,
    config: AppConfig,
    client: Any,
    task: ResourceDiscoverTask,
    *,
    limit: int,
) -> tuple[int, int]:
    """句式搜索：全局搜消息 → 从命中消息里提链接进候选池。"""
    messages = await search_global_messages(client, task.keyword, limit=limit)
    from app.services.resource_probe_service import absorb_links

    created = 0
    for message in messages:
        created += await absorb_links(
            session,
            config,
            text=getattr(message, "message", None),
            source_title=f"句式搜索：{task.keyword}",
            discovered_by=DISCOVER_PHRASE,
        )
    return len(messages), created


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


async def discover_once(
    session: AsyncSession,
    config: AppConfig,
    client: Any,
    *,
    keywords: list[str] | None = None,
    limit: int = 20,
    account_id: int | None = None,
) -> dict[str, Any]:
    """界面上的「采集一次」：跑一批到点任务，或按指定关键词搜一轮。"""
    results: list[dict[str, Any]] = []
    if keywords:
        for keyword in keywords:
            clean = keyword.strip()
            if not clean:
                continue
            try:
                hits, created = await _run_keyword_raw(
                    session,
                    client,
                    clean,
                    limit=limit,
                )
            except Exception as exc:  # noqa: BLE001 - 单个词失败不影响其他词
                results.append({"keyword": clean, "error": str(exc)[:200]})
                continue
            await resource_quota_service.bump(session, account_id, searches=1)
            results.append(
                {"keyword": clean, "hits": hits, "new_resources": created, "error": None}
            )
        return {"results": results}

    task = await next_due_task(session)
    if task is None:
        return {"results": [], "hint": "没有到点的发现任务"}
    outcome = await run_task(
        session,
        config,
        client,
        task,
        limit=limit,
        account_id=account_id,
    )
    return {
        "results": [
            {
                "keyword": outcome.keyword,
                "hits": outcome.hits,
                "new_resources": outcome.new_resources,
                "error": outcome.error,
                "skipped": outcome.skipped,
            }
        ]
    }


async def _run_keyword_raw(
    session: AsyncSession,
    client: Any,
    keyword: str,
    *,
    limit: int,
) -> tuple[int, int]:
    """按关键词搜一轮，不涉及任务表（临时搜索用）。"""
    profiles = await search_public_chats(client, keyword, limit=limit)
    created = await _store_profiles(
        session,
        profiles,
        discovered_by=DISCOVER_KEYWORD,
        discovered_from=f"临时搜索：{keyword}",
    )
    return len(profiles), created


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


def serialize_task(task: ResourceDiscoverTask) -> dict[str, Any]:
    """任务对外结构。"""
    from app.db.base import as_utc

    last_run = as_utc(task.last_run_at)
    next_run = as_utc(task.next_run_at)
    return {
        "id": task.id,
        "kind": task.kind,
        "keyword": task.keyword,
        "category": task.category,
        "enabled": task.enabled,
        "hits": task.hits,
        "new_found": task.new_found,
        "flood_waits": task.flood_waits,
        "last_error": task.last_error,
        "last_run_at": last_run.isoformat() if last_run else None,
        "next_run_at": next_run.isoformat() if next_run else None,
        "due": bool(
            task.enabled and (next_run is None or next_run <= utc_now()),
        ),
    }
