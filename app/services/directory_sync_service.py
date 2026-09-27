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
    TENANT_STATUS_ACTIVE,
    ResourceDirectoryRun,
    ResourceDiscoverTask,
    Tenant,
)
from app.services import resource_quota_service, resource_service
from app.services.resource_service import ResourceRef

# 后台只维护这一条默认目录。tg-me 仍由“在线补搜”按关键词实时抓取，
# 不再进入无人值守的目录任务。
DEFAULT_DIRECTORY_SOURCE = COMBOT
DEFAULT_DIRECTORY_SCOPE = "zh"

# 每轮最多抓 3 页，避免一次同步占用资源发现循环太久。
DIRECTORY_PAGES_PER_TICK = 3
DIRECTORY_CONTINUE_SECONDS = 60
DIRECTORY_RETRY_SECONDS = 900

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


def resume_page(run: ResourceDirectoryRun | None) -> int:
    """下一轮从第几页开始。

    - 没跑过、或上一轮正常跑完 → 从第 1 页开始（重新对齐一遍目录）；
    - 上一轮**没跑完** → 从断点继续；
    - 上一轮虽然记的是 partial，但已经抓满 ``pages_total``（老版本的越界误报）→ 也从头来。
    """
    if run is None:
        return 1
    if run.result == SYNC_OK:
        return 1
    done = max(0, int(run.pages_done or 0))
    total = run.pages_total
    if total is not None and done >= int(total):
        return 1
    return max(1, done + 1)


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
        # 目录规模是实测值：抓满 pages_total 就收工，别去请求越界页
        # （combot 对越界 offset 返回的是非 JSON，会被误判成失败）
        if run.pages_total is not None and page > run.pages_total:
            break
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
        .join(Tenant, Tenant.id == ResourceDiscoverTask.tenant_id)
        .where(
            ResourceDiscoverTask.kind == DISCOVER_DIRECTORY,
            ResourceDiscoverTask.source == DEFAULT_DIRECTORY_SOURCE,
            ResourceDiscoverTask.category == DEFAULT_DIRECTORY_SCOPE,
            ResourceDiscoverTask.enabled.is_(True),
            Tenant.runtime_enabled.is_(True),
            Tenant.status == TENANT_STATUS_ACTIVE,
            (Tenant.expires_at.is_(None)) | (Tenant.expires_at > moment),
        )
        .where(
            (ResourceDiscoverTask.next_run_at.is_(None))
            | (ResourceDiscoverTask.next_run_at <= moment)
        )
        .order_by(ResourceDiscoverTask.next_run_at.asc().nulls_first())
    )


async def ensure_default_task(
    session: AsyncSession,
    *,
    tenant_id: int,
    commit: bool = True,
) -> ResourceDiscoverTask:
    """确保租户有一条 Combot / 中文榜后台同步任务。"""
    existing = await session.scalar(
        select(ResourceDiscoverTask).where(
            ResourceDiscoverTask.tenant_id == int(tenant_id),
            ResourceDiscoverTask.kind == DISCOVER_DIRECTORY,
            ResourceDiscoverTask.source == DEFAULT_DIRECTORY_SOURCE,
            ResourceDiscoverTask.category == DEFAULT_DIRECTORY_SCOPE,
        )
    )
    if existing is not None:
        existing.enabled = True
        if existing.next_run_at is None:
            existing.next_run_at = utc_now()
        if commit:
            await session.commit()
            await session.refresh(existing)
        else:
            await session.flush()
        return existing

    task = ResourceDiscoverTask(
        kind=DISCOVER_DIRECTORY,
        keyword=f"{DEFAULT_DIRECTORY_SOURCE}:{DEFAULT_DIRECTORY_SCOPE}"[:64],
        category=DEFAULT_DIRECTORY_SCOPE,
        source=DEFAULT_DIRECTORY_SOURCE,
        enabled=True,
        next_run_at=utc_now(),
        tenant_id=int(tenant_id),
    )
    session.add(task)
    if commit:
        await session.commit()
        await session.refresh(task)
    else:
        await session.flush()
    return task


async def run_task(
    session: AsyncSession,
    config: AppConfig,
    task: ResourceDiscoverTask,
    *,
    fetcher: Any,
    max_pages: int | None = DIRECTORY_PAGES_PER_TICK,
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
    task.hits += int(run.get("items_seen") or 0)
    task.new_found += int(run.get("items_added") or 0)
    task.last_error = run.get("error")

    run_result = run.get("result")
    if run_result == SYNC_PARTIAL and "额度" in str(run.get("error") or ""):
        # 今日总量已满：不要每分钟重试，等下一个自然日再继续。
        task.next_run_at = next_run_at(config, base=now)
    elif run_result == SYNC_PARTIAL:
        task.next_run_at = now + timedelta(seconds=DIRECTORY_CONTINUE_SECONDS)
    elif run_result == SYNC_FAILED:
        task.next_run_at = now + timedelta(seconds=DIRECTORY_RETRY_SECONDS)
    else:
        task.next_run_at = next_run_at(config, base=now)

    await session.commit()
    await session.refresh(task)
    return {
        **outcome,
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


async def overview(session: AsyncSession, config: AppConfig) -> dict[str, Any]:
    """资源发现页使用的 Combot 后台同步状态（纯读库）。"""
    section = config.resource
    task = await session.scalar(
        select(ResourceDiscoverTask).where(
            ResourceDiscoverTask.kind == DISCOVER_DIRECTORY,
            ResourceDiscoverTask.source == DEFAULT_DIRECTORY_SOURCE,
            ResourceDiscoverTask.category == DEFAULT_DIRECTORY_SCOPE,
        )
    )
    run = await last_run(session, DEFAULT_DIRECTORY_SOURCE, DEFAULT_DIRECTORY_SCOPE)
    return {
        "enabled": section.directory_enabled,
        "source": DEFAULT_DIRECTORY_SOURCE,
        "scope": DEFAULT_DIRECTORY_SCOPE,
        "pages_total": pages_total(
            DEFAULT_DIRECTORY_SOURCE,
            DEFAULT_DIRECTORY_SCOPE,
            page_size=section.directory_page_size,
        ),
        "auto": bool(task and task.enabled),
        "next_run_at": as_utc(task.next_run_at).isoformat() if task and task.next_run_at else None,
        "last_run": serialize_run(run) if run else None,
    }
