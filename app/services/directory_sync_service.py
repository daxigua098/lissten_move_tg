"""目录同步服务：把 combot / tg-me 的目录拉进候选池（F-R20 / F-R21 / F-R24）。

三条硬规则：

1. **只做发现**：目录站给的成员数进 ``directory_member_count``，**不覆盖**探测出来的
   ``member_count``；有数字 ID 的（combot）按 ``tg_id`` 归一，没有的（tg-me）靠
   ``username`` / 邀请链接去重，等探测再归一。
2. **断点续抓**：每抓完一页就写一次 ``resource_directory_runs.pages_done``，
   中断后再同步从 ``pages_done + 1`` 继续，不重复抓已完成的页。
3. **额度与限速**：每日请求数上限 ``directory_daily_requests``，两次请求之间有间隔；
   超限就停下并记 ``partial``，不报错也不硬闯。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.directory_client import DirectoryFetchError
from app.core.directory_sites import (
    COMBOT,
    DIRECTORY_SITES,
    PAGE_SIZE,
    SCOPE_GLOBAL,
    TGME,
    TGME_MIRROR,
    TGME_WWW,
    DirectoryEntry,
    combot_page_url,
    pages_total,
    parse_page,
    tgme_page_url,
)
from app.core.errors import ValidationFailedError
from app.core.resource_probe import detect_content_rating
from app.db.base import as_utc, utc_now
from app.db.models import (
    DISCOVER_DIRECTORY,
    SYNC_FAILED,
    SYNC_OK,
    SYNC_PARTIAL,
    ResourceDirectoryRun,
    ResourceDiscoverTask,
)
from app.services import resource_quota_service, resource_service
from app.services.resource_service import ResourceRef

SITE_LABELS = {
    COMBOT: "Combot 目录",
    TGME: "tg-me 列表页",
}


# ------------------------------------------------------------------ 查询


async def daily_requests_used(session: AsyncSession, *, since: Any = None) -> int:
    """今天已经用掉的目录请求数（配额看板与同步上限都用它）。"""
    moment = since or resource_quota_service.today()
    total = await session.scalar(
        select(func.coalesce(func.sum(ResourceDirectoryRun.requests_used), 0)).where(
            ResourceDirectoryRun.started_at >= moment
        )
    )
    return int(total or 0)


async def last_run(
    session: AsyncSession,
    source: str,
    scope: str,
) -> ResourceDirectoryRun | None:
    """某个站点某个范围最近一次同步记录（断点续抓靠它）。"""
    return await session.scalar(
        select(ResourceDirectoryRun)
        .where(
            ResourceDirectoryRun.source == source,
            ResourceDirectoryRun.scope == scope,
        )
        .order_by(ResourceDirectoryRun.started_at.desc(), ResourceDirectoryRun.id.desc())
    )


async def list_runs(
    session: AsyncSession,
    *,
    source: str | None = None,
    scope: str | None = None,
    limit: int = 50,
) -> list[ResourceDirectoryRun]:
    """同步历史（P-R05 的表）。"""
    statement = select(ResourceDirectoryRun)
    if source:
        statement = statement.where(ResourceDirectoryRun.source == source)
    if scope:
        statement = statement.where(ResourceDirectoryRun.scope == scope)
    statement = statement.order_by(ResourceDirectoryRun.started_at.desc())
    return list(await session.scalars(statement.limit(max(1, limit))))


def resume_page(run: ResourceDirectoryRun | None) -> int:
    """下一轮从第几页开始：上一轮**没跑完**才续点，正常跑完的从头来。"""
    if run is None:
        return 1
    if run.result == SYNC_OK:
        return 1
    return max(1, int(run.pages_done or 0) + 1)


def next_run_at(config: AppConfig, *, base: Any = None):
    """下一次目录任务的计划时间。"""
    return (base or utc_now()) + timedelta(hours=config.resource.directory_sync_hours)


# ------------------------------------------------------------------ 抓取


async def fetch_page(
    fetcher: Any,
    source: str,
    scope: str,
    page: int,
    *,
    page_size: int = PAGE_SIZE,
) -> list[DirectoryEntry]:
    """抓一页并解析成条目；tg-me 失败时自动换镜像域名再试一次。"""
    if source == COMBOT:
        offset = max(0, (page - 1) * page_size)
        url = combot_page_url(scope, offset, limit=page_size)
        return parse_page(source, await fetcher.fetch(url), scope=scope)
    if source == TGME:
        errors: list[str] = []
        for base in (TGME_WWW, TGME_MIRROR):
            url = tgme_page_url(scope, page, base=base)
            try:
                return parse_page(source, await fetcher.fetch(url), scope=scope)
            except DirectoryFetchError as exc:  # 主站挂了就试镜像
                errors.append(str(exc))
        raise DirectoryFetchError("；".join(errors))
    raise ValidationFailedError(f"不认识的目录站：{source}")


# ------------------------------------------------------------------ 同步


async def sync_once(
    session: AsyncSession,
    config: AppConfig,
    source: str,
    scope: str = SCOPE_GLOBAL,
    *,
    fetcher: Any,
    max_pages: int | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    """同步一个站的一个范围。返回本轮结果摘要（同时写一条 run 记录）。"""
    if source not in DIRECTORY_SITES:
        raise ValidationFailedError(f"目录站必须是 {'/'.join(DIRECTORY_SITES)} 之一")
    section = config.resource
    if not section.directory_enabled:
        return {"result": "skipped", "reason": "disabled"}

    clean_scope = (scope or SCOPE_GLOBAL).strip() or SCOPE_GLOBAL
    previous = await last_run(session, source, clean_scope) if resume else None
    start_page = resume_page(previous)

    used = await daily_requests_used(session)
    remaining = section.directory_daily_requests - used
    if remaining <= 0:
        return {
            "result": "skipped",
            "reason": "quota",
            "daily_requests_used": used,
            "daily_requests_limit": section.directory_daily_requests,
        }

    run = ResourceDirectoryRun(
        source=source,
        scope=clean_scope,
        started_at=utc_now(),
        pages_done=start_page - 1,
        pages_total=pages_total(source, clean_scope, page_size=section.directory_page_size),
        result=SYNC_OK,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)

    page = start_page
    done_in_call = 0
    result = SYNC_OK
    error: str | None = None
    while True:
        if max_pages is not None and done_in_call >= max_pages:
            result = SYNC_PARTIAL
            break
        if run.requests_used >= remaining:
            result = SYNC_PARTIAL
            error = "今日目录请求额度已用完，剩下的页下次继续"
            break
        try:
            entries = await fetch_page(
                fetcher,
                source,
                clean_scope,
                page,
                page_size=section.directory_page_size,
            )
        except Exception as exc:  # noqa: BLE001 - 抓取失败要落库，不能中断整轮
            error = str(exc)[:255]
            result = SYNC_PARTIAL if done_in_call else SYNC_FAILED
            logger.warning("目录同步 {}/{} 第 {} 页失败：{}", source, clean_scope, page, exc)
            break

        run.requests_used += 1
        if not entries:
            # 空页 = 这个范围到底了
            break

        added = await store_entries(
            session,
            entries,
            source=source,
            scope=clean_scope,
            page=page,
        )
        run.items_seen += len(entries)
        run.items_added += added
        run.pages_done = page
        await session.commit()

        done_in_call += 1
        page += 1
        if section.directory_request_interval:
            await asyncio.sleep(section.directory_request_interval)

    run.finished_at = utc_now()
    run.result = result
    run.error = error
    await session.commit()
    await session.refresh(run)
    return {
        "result": result,
        "run": serialize_run(run),
        "resumed_from": start_page if start_page > 1 else None,
    }


async def store_entries(
    session: AsyncSession,
    entries: list[DirectoryEntry],
    *,
    source: str,
    scope: str,
    page: int,
) -> int:
    """把一页条目写进候选池，返回新增条数（在线补搜也用它）。"""
    label = SITE_LABELS.get(source, source)
    discovered_from = f"{label}：{scope} 第 {page} 页"
    created = 0
    for entry in entries:
        if entry.tg_id is None and not entry.username and not entry.invite_link:
            # 既没有数字 ID 也没有链接的条目没法去重，宁可不要
            continue
        outcome = await resource_service.upsert_resource(
            session,
            ResourceRef(
                title=entry.title,
                tg_id=entry.tg_id,
                username=entry.username,
                invite_link=entry.invite_link,
                chat_type=entry.chat_type,
                member_count_approx=True,
                source_url=entry.source_url,
                source_site=entry.source_site,
                language=entry.language,
                directory_rank=entry.rank,
                directory_member_count=entry.member_count,
                # 入库时的粗判：只看标题；探测后按采样消息复判（F-R22）
                content_rating=detect_content_rating(entry.title),
            ),
            discovered_by=DISCOVER_DIRECTORY,
            discovered_from=discovered_from[:255],
        )
        if outcome.created:
            created += 1
    return created


# ------------------------------------------------------------------ 任务


async def next_due_task(session: AsyncSession) -> ResourceDiscoverTask | None:
    """取一个到点的目录任务（运行时循环用）。"""
    moment = utc_now()
    return await session.scalar(
        select(ResourceDiscoverTask)
        .where(
            ResourceDiscoverTask.kind == DISCOVER_DIRECTORY,
            ResourceDiscoverTask.enabled.is_(True),
        )
        .where(
            (ResourceDiscoverTask.next_run_at.is_(None))
            | (ResourceDiscoverTask.next_run_at <= moment)
        )
        .order_by(ResourceDiscoverTask.next_run_at.asc().nulls_first())
    )


async def create_task(
    session: AsyncSession,
    *,
    source: str,
    scope: str = SCOPE_GLOBAL,
    enabled: bool = True,
) -> ResourceDiscoverTask:
    """建一个目录同步任务（同站同范围只允许一条）。"""
    if source not in DIRECTORY_SITES:
        raise ValidationFailedError(f"目录站必须是 {'/'.join(DIRECTORY_SITES)} 之一")
    clean_scope = (scope or SCOPE_GLOBAL).strip() or SCOPE_GLOBAL
    existing = await session.scalar(
        select(ResourceDiscoverTask).where(
            ResourceDiscoverTask.kind == DISCOVER_DIRECTORY,
            ResourceDiscoverTask.source == source,
            ResourceDiscoverTask.category == clean_scope,
        )
    )
    if existing is not None:
        return existing
    task = ResourceDiscoverTask(
        kind=DISCOVER_DIRECTORY,
        keyword=f"{source}:{clean_scope}"[:64],
        category=clean_scope,
        source=source,
        enabled=enabled,
        next_run_at=utc_now(),
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def run_task(
    session: AsyncSession,
    config: AppConfig,
    task: ResourceDiscoverTask,
    *,
    fetcher: Any,
    max_pages: int | None = None,
) -> dict[str, Any]:
    """跑一个目录任务，并回写任务表的计数与下次执行时间。"""
    if task.kind != DISCOVER_DIRECTORY:
        raise ValidationFailedError("这不是目录任务")
    source = task.source
    if not source:
        raise ValidationFailedError("目录任务必须指定 source")
    scope = task.category or task.keyword.split(":", 1)[-1] or SCOPE_GLOBAL

    now = utc_now()
    outcome = await sync_once(
        session,
        config,
        source,
        scope,
        fetcher=fetcher,
        max_pages=max_pages,
    )
    run = outcome.get("run") or {}
    task.last_run_at = now
    task.next_run_at = next_run_at(config, base=now)
    task.hits += int(run.get("items_seen") or 0)
    task.new_found += int(run.get("items_added") or 0)
    task.last_error = run.get("error")
    await session.commit()
    await session.refresh(task)
    return {
        **outcome,
        "task": serialize_task(task),
        "source": source,
        "scope": scope,
    }


# ------------------------------------------------------------------ 序列化


def serialize_run(run: ResourceDirectoryRun) -> dict[str, Any]:
    """同步记录的对外结构。"""
    started = as_utc(run.started_at)
    finished = as_utc(run.finished_at)
    return {
        "id": run.id,
        "source": run.source,
        "scope": run.scope,
        "started_at": started.isoformat() if started else None,
        "finished_at": finished.isoformat() if finished else None,
        "pages_done": run.pages_done,
        "pages_total": run.pages_total,
        "items_seen": run.items_seen,
        "items_added": run.items_added,
        "requests_used": run.requests_used,
        "result": run.result,
        "error": run.error,
        "resumable": run.result != SYNC_OK and int(run.pages_done or 0) > 0,
    }


def serialize_task(task: ResourceDiscoverTask) -> dict[str, Any]:
    """目录任务的对外结构。"""
    last_run = as_utc(task.last_run_at)
    next_run = as_utc(task.next_run_at)
    return {
        "id": task.id,
        "kind": task.kind,
        "source": task.source,
        "keyword": task.keyword,
        "category": task.category,
        "enabled": task.enabled,
        "hits": task.hits,
        "new_found": task.new_found,
        "flood_waits": task.flood_waits,
        "last_error": task.last_error,
        "last_run_at": last_run.isoformat() if last_run else None,
        "next_run_at": next_run.isoformat() if next_run else None,
        "due": bool(task.enabled and (next_run is None or next_run <= utc_now())),
    }


async def overview(session: AsyncSession, config: AppConfig) -> dict[str, Any]:
    """站点与范围的状态（P-R05 的站点卡片）。"""
    section = config.resource
    configured = set(section.directory_sites)
    # 目录任务里出现过的范围也算进来，手动加过的范围不会从界面上消失
    task_scopes = [
        item
        for item in await session.scalars(
            select(ResourceDiscoverTask.category)
            .where(ResourceDiscoverTask.kind == DISCOVER_DIRECTORY)
            .distinct()
        )
        if item
    ]
    scopes = [item for item in dict.fromkeys([*section.directory_scopes, *task_scopes]) if item]

    sites: list[dict[str, Any]] = []
    for source in DIRECTORY_SITES:
        scopes_payload: list[dict[str, Any]] = []
        for scope in scopes:
            run = await last_run(session, source, scope)
            scopes_payload.append(
                {
                    "scope": scope,
                    "pages_total": pages_total(
                        source,
                        scope,
                        page_size=section.directory_page_size,
                    ),
                    "last_run": serialize_run(run) if run else None,
                }
            )
        sites.append(
            {
                "source": source,
                "label": SITE_LABELS.get(source, source),
                "enabled": bool(section.directory_enabled and source in configured),
                "scopes": scopes_payload,
            }
        )

    return {
        "enabled": section.directory_enabled,
        "sites": sites,
        "page_size": section.directory_page_size,
        "daily_requests_used": await daily_requests_used(session),
        "daily_requests_limit": section.directory_daily_requests,
        "sync_hours": section.directory_sync_hours,
    }
