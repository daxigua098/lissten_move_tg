"""资源发现：资源库、采集任务、加群队列与采纳（P-R01 / P-R03）。

一条铁律：**列表 / 筛选 / 导出只读本地库**，不产生任何 Telegram 请求
（验收标准 1）。要访问 Telegram 的动作都写成显式的 POST：采集、刷新、加入、采纳。
"""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity, require_role, session_dependency
from app.api.schemas.resource import (
    DirectorySyncRequest,
    DirectoryTaskRequest,
    OnlineSearchRequest,
    ResourceAdoptRequest,
    ResourceImportRequest,
    ResourceJoinRequest,
    ResourceRefreshRequest,
    ResourceUpdateRequest,
)
from app.core.config import AppConfig
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.heartbeat import heartbeat_age_seconds, is_running, read_status
from app.core.telegram_client import ChatProfile
from app.db.models import ROLE_SUB_ADMIN
from app.services import (
    chat_service,
    chat_sync_service,
    directory_sync_service,
    resource_discover_service,
    resource_join_service,
    resource_probe_service,
    resource_quota_service,
    resource_service,
    route_service,
)

router = APIRouter(
    prefix="/api/resources",
    tags=["resources"],
    dependencies=[Depends(require_role(ROLE_SUB_ADMIN))],
)


def _client_factory(request: Request) -> Any:
    return getattr(request.app.state, "account_client_factory", None)


def _directory_fetcher(request: Request) -> Any:
    """目录站抓取器（测试通过 app.state.directory_fetcher_factory 注入替身）。"""
    factory = getattr(request.app.state, "directory_fetcher_factory", None)
    if factory is not None:
        return factory()
    from app.core.directory_client import DirectoryFetcher

    return DirectoryFetcher()


def _config(request: Request) -> AppConfig:
    return request.app.state.config


# ------------------------------------------------------------------ 列表与看板


@router.get("")
async def list_resources(
    keyword: str | None = Query(default=None),
    chat_type: str | None = Query(default=None),
    languages: list[str] | None = Query(default=None),
    categories: list[str] | None = Query(default=None),
    member_min: int | None = Query(default=None, ge=0),
    member_max: int | None = Query(default=None, ge=0),
    min_activity: float | None = Query(default=None, ge=0, le=100),
    is_index_group: bool | None = Query(default=None),
    freshness: str | None = Query(default=None, pattern="^(fresh|warm|stale|new)$"),
    active_within_days: int | None = Query(default=None, ge=1, le=3650),
    adopted: bool | None = Query(default=None),
    blacklisted: bool | None = Query(default=None),
    favorite: bool | None = Query(default=None),
    status: str | None = Query(default=None),
    source_site: str | None = Query(default=None, pattern="^(telegram|combot|tgme|manual)$"),
    content_rating: str | None = Query(
        default=None,
        pattern="^(normal|sensitive|unknown)$",
    ),
    include_sensitive: bool = Query(default=False),
    due_refresh: bool = Query(default=False),
    sort: str = Query(default="activity"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """资源库列表（纯读库）。"""
    rows, total = await resource_service.list_resources(
        session,
        keyword=keyword,
        chat_type=chat_type,
        languages=languages,
        categories=categories,
        member_min=member_min,
        member_max=member_max,
        min_activity=min_activity,
        is_index_group=is_index_group,
        freshness=freshness,
        active_within_days=active_within_days,
        adopted=adopted,
        blacklisted=blacklisted,
        favorite=favorite,
        status=status,
        source_site=source_site,
        content_rating=content_rating,
        include_sensitive=include_sensitive,
        due_refresh=due_refresh,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return {
        "items": await _serialize_many(session, rows),
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def _serialize_many(session: AsyncSession, rows: Any) -> list[dict[str, Any]]:
    """序列化一批资源，并把「让账号加入」的最近状态一起带上。"""
    items = [resource_service.serialize_resource(row) for row in rows]
    await resource_service.attach_join_states(session, items)
    return items


@router.get("/overview")
async def resource_overview(
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """顶部看板：配额、队列长度与资源库概览（F-R16）。"""
    config = _config(request)
    quota = await resource_quota_service.today_total(session)
    stats = await resource_service.stats(session)
    join_stats = await resource_join_service.stats(session)
    searches_left = max(0, config.resource.search_daily_limit - quota["searches"])
    return {
        "searches_today": quota["searches"],
        "searches_left": searches_left,
        "search_daily_limit": config.resource.search_daily_limit,
        "joins_today": quota["joins"],
        "join_daily_limit": config.resource.join_daily_limit,
        "leaves_today": quota["leaves"],
        "probes_today": quota["probes"],
        "flood_waits_today": quota["flood_waits"],
        "refresh_queue": await resource_service.refresh_queue_size(session),
        "join_queue": join_stats.get("pending", 0),
        "join_waiting_approval": join_stats.get("waiting_approval", 0),
        "resources": stats,
        # 加群与探测都由运行时执行：它没在跑的话，队列只会越积越多
        "runtime": _runtime_snapshot(config),
    }


def _runtime_snapshot(config: AppConfig) -> dict[str, Any]:
    """运行时是否真的在跑（看心跳新鲜度，不看那条可能过期的状态字符串）。"""
    heartbeat = read_status(config.path(config.runtime.status_file))
    return {
        "status": (heartbeat or {}).get("status", "stopped"),
        "heartbeat_age_seconds": heartbeat_age_seconds(heartbeat),
        "running": is_running(heartbeat),
    }


@router.get("/facets")
async def resource_facets(session: AsyncSession = Depends(session_dependency)) -> dict[str, Any]:
    """筛选项候选（语言、行业）。"""
    facets = await resource_service.facet_options(session)
    return {
        **facets,
        "chat_types": [
            {"value": "channel", "label": "频道"},
            {"value": "supergroup", "label": "超级群"},
            {"value": "group", "label": "群组"},
        ],
        "freshness": [
            {"value": "fresh", "label": "24 小时内"},
            {"value": "warm", "label": "7 天内"},
            {"value": "stale", "label": "超过 7 天"},
            {"value": "new", "label": "还没探测"},
        ],
        "sorts": [
            {"value": "activity", "label": "真人活跃度"},
            {"value": "members", "label": "成员数"},
            {"value": "potential", "label": "线索潜力"},
            {"value": "recent_found", "label": "最近发现"},
            {"value": "recent_refresh", "label": "最近刷新"},
        ],
    }


@router.get("/counts")
async def resource_counts(session: AsyncSession = Depends(session_dependency)) -> dict[str, Any]:
    """卡片墙的分类 chips 与快捷榜计数（纯读库，F-R23）。"""
    return await resource_service.counts(session)


@router.get("/export.csv")
async def export_resources(
    keyword: str | None = Query(default=None),
    chat_type: str | None = Query(default=None),
    languages: list[str] | None = Query(default=None),
    categories: list[str] | None = Query(default=None),
    member_min: int | None = Query(default=None, ge=0),
    member_max: int | None = Query(default=None, ge=0),
    min_activity: float | None = Query(default=None, ge=0, le=100),
    is_index_group: bool | None = Query(default=None),
    freshness: str | None = Query(default=None),
    adopted: bool | None = Query(default=None),
    blacklisted: bool | None = Query(default=None),
    favorite: bool | None = Query(default=None),
    status: str | None = Query(default=None),
    source_site: str | None = Query(default=None, pattern="^(telegram|combot|tgme|manual)$"),
    content_rating: str | None = Query(
        default=None,
        pattern="^(normal|sensitive|unknown)$",
    ),
    include_sensitive: bool = Query(default=True),
    sort: str = Query(default="activity"),
    session: AsyncSession = Depends(session_dependency),
) -> Response:
    """按当前筛选条件导出 CSV（含全部指标与来源路径）。"""
    rows, _total = await resource_service.list_resources(
        session,
        keyword=keyword,
        chat_type=chat_type,
        languages=languages,
        categories=categories,
        member_min=member_min,
        member_max=member_max,
        min_activity=min_activity,
        is_index_group=is_index_group,
        freshness=freshness,
        adopted=adopted,
        blacklisted=blacklisted,
        favorite=favorite,
        status=status,
        source_site=source_site,
        content_rating=content_rating,
        include_sensitive=include_sensitive,
        sort=sort,
        limit=5000,
        offset=0,
    )
    return Response(
        content=resource_service.resources_to_csv(rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="resources.csv"'},
    )


# ------------------------------------------------------------------ 加群队列


@router.get("/directory/sources")
async def directory_sources(
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """目录站与范围的状态（P-R05 的站点卡片，纯读库）。"""
    return await directory_sync_service.overview(session, _config(request))


@router.get("/directory/runs")
async def directory_runs(
    source: str | None = Query(default=None),
    scope: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """目录同步历史（P-R05 的表）。"""
    rows = await directory_sync_service.list_runs(
        session,
        source=source,
        scope=scope,
        limit=limit,
    )
    return {"items": [directory_sync_service.serialize_run(row) for row in rows]}


@router.post("/directory/sync")
async def directory_sync(
    payload: DirectorySyncRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """同步一次目录站（显式动作，会出网；默认从上次的断点继续）。"""
    fetcher = _directory_fetcher(request)
    try:
        return await directory_sync_service.sync_once(
            session,
            _config(request),
            payload.source,
            payload.scope,
            fetcher=fetcher,
            max_pages=payload.max_pages,
            resume=payload.resume,
        )
    finally:
        with contextlib.suppress(Exception):
            await fetcher.aclose()


@router.post("/directory/tasks", status_code=201)
async def create_directory_task(
    payload: DirectoryTaskRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """把某个站点某个范围设为「每天自动同步」（同站同范围幂等）。"""
    task = await directory_sync_service.create_task(
        session,
        source=payload.source,
        scope=payload.scope,
        enabled=payload.enabled,
    )
    return directory_sync_service.serialize_task(task)


@router.delete("/directory/tasks/{task_id}")
async def delete_directory_task(
    task_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """取消「每天自动同步」（已经同步下来的资源不动）。"""
    deleted = await directory_sync_service.delete_task(session, task_id)
    if not deleted:
        raise NotFoundError("目录任务不存在")
    return {"id": task_id, "deleted": True}


# ------------------------------------------------------------------ 显式动作


@router.post("/discover-online")
async def discover_online(
    payload: OnlineSearchRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """在线补搜（F-R19）：本地结果已经先渲染，这一步只追加新发现的。

    单渠道失败不影响其它渠道；Telegram 那一路没有可用账号时其余渠道照常返回。
    """
    config = _config(request)
    fetcher = _directory_fetcher(request)
    account: Any = None
    client: Any = None
    try:
        with contextlib.suppress(Exception):
            account, client = await _open_client(
                session,
                config,
                account_id=payload.account_id,
                client_factory=_client_factory(request),
            )
        return await resource_discover_service.search_online(
            session,
            config,
            client,
            keywords=payload.keywords,
            sites=payload.sites,
            limit=payload.limit,
            account_id=account.id if account else None,
            directory_fetcher=fetcher,
        )
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
        with contextlib.suppress(Exception):
            await fetcher.aclose()


@router.post("/import", status_code=201)
async def import_resources(
    payload: ResourceImportRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """手动添加（F-R11）：粘贴链接 / 用户名 / ID，入库后立即探测。"""
    config = _config(request)
    account, client = await _open_client(
        session,
        config,
        account_id=payload.account_id,
        client_factory=_client_factory(request),
    )
    try:
        return await resource_probe_service.import_resources(
            session,
            config,
            client,
            payload.inputs,
            join=payload.join,
            account_id=account.id if account else None,
            probe=payload.probe,
        )
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


@router.post("/refresh")
async def refresh_resources(
    payload: ResourceRefreshRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """批量刷新：单行「刷新」与勾选后「批量刷新」共用这个接口（F-R10）。"""
    config = _config(request)
    resources = []
    for resource_id in payload.ids:
        resource = await resource_service.get_resource(session, resource_id)
        if resource is None:
            raise NotFoundError(f"资源 {resource_id} 不存在")
        resources.append(resource)

    account, client = await _open_client(
        session,
        config,
        account_id=payload.account_id,
        client_factory=_client_factory(request),
    )
    try:
        result = await resource_probe_service.probe_many(
            session,
            config,
            client,
            resources,
            account_id=account.id if account else None,
            sample_depth=payload.sample_depth,
        )
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
    return {"account": account.name if account else None, **result}


@router.post("/join", status_code=201)
async def enqueue_join(
    payload: ResourceJoinRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """批量加入群组池 / 退出（F-R12 / F-R14）：只入队，由限速器按节奏执行。"""
    config = _config(request)
    queued: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for resource_id in payload.ids:
        resource = await resource_service.get_resource(session, resource_id)
        if resource is None:
            failures.append({"id": resource_id, "reason": "资源不存在"})
            continue
        try:
            task = await resource_join_service.enqueue(
                session,
                config,
                resource,
                account_id=payload.account_id,
                action=payload.action,
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响其他条
            failures.append({"id": resource_id, "reason": str(getattr(exc, "detail", exc))})
            continue
        queued.append(resource_join_service.serialize_task(task, resource))
    return {"queued": queued, "failures": failures}


# ------------------------------------------------------------------ 单条资源


@router.get("/{resource_id}")
async def resource_detail(
    resource_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """资源详情：指标、样本消息预览、探测历史（趋势）与来源路径。"""
    resource = await resource_service.require_resource(session, resource_id)
    payload = resource_service.serialize_resource(resource)
    await resource_service.attach_join_states(session, [payload])
    payload["samples"] = resource_service.load_samples(resource)
    payload["title_history"] = resource_service.load_title_history(resource)
    logs = await resource_service.probe_history(session, resource_id)
    payload["history"] = [resource_service.serialize_probe_log(row) for row in logs]
    return payload


@router.post("/{resource_id}/refresh")
async def refresh_one(
    resource_id: int,
    request: Request,
    account_id: int | None = Query(default=None),
    sample_depth: int | None = Query(default=None, ge=5, le=500),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """刷新单条资源。"""
    config = _config(request)
    resource = await resource_service.require_resource(session, resource_id)
    account, client = await _open_client(
        session,
        config,
        account_id=account_id,
        client_factory=_client_factory(request),
    )
    try:
        outcome = await resource_probe_service.probe_resource(
            session,
            config,
            client,
            resource,
            account_id=account.id if account else None,
            sample_depth=sample_depth,
        )
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
    return {
        "id": resource_id,
        "result": outcome.result,
        "error": outcome.error,
        "new_resources": outcome.new_resources,
        "resource": resource_service.serialize_resource(outcome.resource),
    }


@router.patch("/{resource_id}")
async def update_resource(
    resource_id: int,
    payload: ResourceUpdateRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """人工修正字段（会被锁定）、收藏、黑名单与备注。"""
    resource = await resource_service.require_resource(session, resource_id)
    if (
        payload.language is not None
        or payload.country is not None
        or payload.categories is not None
        or payload.note is not None
        or payload.content_rating is not None
    ):
        resource = await resource_service.update_manual_fields(
            session,
            resource,
            language=payload.language,
            country=payload.country,
            categories=payload.categories,
            note=payload.note,
            content_rating=payload.content_rating,
        )
    if payload.is_favorite is not None:
        resource = await resource_service.set_favorite(session, resource, payload.is_favorite)
    if payload.is_blacklisted is not None:
        resource = await resource_service.set_blacklisted(
            session,
            resource,
            payload.is_blacklisted,
            reason=payload.blacklist_reason,
        )
    if payload.status is not None:
        resource = await resource_service.set_status(session, resource, payload.status)
    return resource_service.serialize_resource(resource)


@router.post("/{resource_id}/adopt")
async def adopt_resource(
    resource_id: int,
    payload: ResourceAdoptRequest,
    request: Request,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """采纳（F-R13）：写进监听源，可选顺手建一条 A/B 线，并排入加群队列。"""
    config = _config(request)
    resource = await resource_service.require_resource(session, resource_id)
    if resource.is_blacklisted:
        raise ValidationFailedError("该资源在黑名单里，不能采纳")
    if resource.tg_id is None:
        raise ValidationFailedError("该资源还没有解析出数字 ID，请先探测一次再采纳")

    chat = await _ensure_source_chat(session, resource)
    routes: list[dict[str, Any]] = []
    if payload.create_route:
        if not payload.target_chat_ids:
            raise ValidationFailedError("建线需要至少选一个接收目标")
        created = await route_service.create_route_bundle(
            session,
            name=payload.route_name
            or f"{resource.title or resource.username} → {payload.business_type} 线",
            source_chat_ids=[chat.id],
            business_type=payload.business_type,
            target_chat_ids=payload.target_chat_ids,
            created_by=str(identity.get("username")),
        )
        routes = [{"id": item.id, "name": item.name} for item in created]

    # 先排队再加"已采纳"标记：加群队列会拒绝已采纳的资源，顺序反了就排不进去
    queued = None
    if not payload.defer_join and resource.resource_state != "active":
        # 还没确认在群里：排进加群队列（限速执行），而不是立刻猛加
        try:
            task = await resource_join_service.enqueue(
                session,
                config,
                resource,
                account_id=payload.account_id,
            )
            queued = resource_join_service.serialize_task(task, resource)
        except Exception as exc:  # noqa: BLE001 - 采纳本身已经成功，排队失败只提示
            queued = {"error": str(getattr(exc, "detail", exc))}

    resource = await resource_service.mark_adopted(
        session,
        resource,
        adopted_by=str(identity.get("username")),
        account_id=payload.account_id,
        review_days=config.resource.review_days,
    )

    return {
        "resource": resource_service.serialize_resource(resource),
        "chat_id": chat.id,
        "routes": routes,
        "join_task": queued,
    }


async def _ensure_source_chat(session: AsyncSession, resource: Any) -> Any:
    """把资源写成监听源（复用群组池，不新建一套表）。"""
    profile = ChatProfile(
        tg_id=int(resource.tg_id),
        chat_type=resource.chat_type or "group",
        title=resource.title,
        username=resource.username,
        is_private=not bool(resource.username),
        member_count=resource.member_count,
    )
    chat = await chat_service.upsert_chat_from_profile(session, profile, joined=True)
    return await chat_service.set_source(session, chat, enabled=True)


async def _open_client(
    session: AsyncSession,
    config: AppConfig,
    *,
    account_id: int | None,
    client_factory: Any,
) -> tuple[Any, Any]:
    """取执行账号并打开客户端。"""
    return await chat_sync_service.open_account_client(
        session,
        config,
        account_id=account_id,
        client_factory=client_factory,
    )
