"""线路管理：矩阵建线、配置、目标与水位线。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity, require_role, session_dependency
from app.api.routers.chats import TARGET_ROLE_LABEL
from app.api.schemas.route import (
    EnabledUpdate,
    ResetProgressRequest,
    RouteCreateRequest,
    RouteMatrixRequest,
    RouteTargetAddRequest,
    RouteUpdateRequest,
)
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.route_config import load_a_config, load_b_config
from app.db.base import as_utc
from app.db.models import ROLE_SUB_ADMIN, Route
from app.services import chat_service, route_service

router = APIRouter(
    prefix="/api/routes",
    tags=["routes"],
    dependencies=[Depends(require_role(ROLE_SUB_ADMIN))],
)


async def serialize_route(session: AsyncSession, route: Route) -> dict[str, Any]:
    """线路详情：源、目标、水位线与 A/B 配置。"""
    source = await chat_service.get_chat(session, route.source_chat_id)
    target_rows = await route_service.list_route_targets(session, route.id)
    progress = {
        (item.route_id, item.target_chat_id): item
        for item in await route_service.list_progress(session, route.id)
    }

    targets: list[dict[str, Any]] = []
    warnings: list[str] = []
    for row in target_rows:
        chat = await chat_service.get_chat(session, row.target_chat_id)
        if chat is None:
            continue
        item = progress.get((route.id, row.target_chat_id))
        if chat.can_post is False:
            warnings.append(f"接收目标「{chat.title or chat.tg_id}」没有发帖权限")
        targets.append(
            {
                "chat_id": chat.id,
                "title": chat.title,
                "display_name": chat.display_name,
                "name": chat.display_name or chat.title or chat.username or f"#{chat.tg_id}",
                "username": chat.username,
                "chat_type": chat.chat_type,
                "is_private": chat.is_private,
                "can_post": chat.can_post,
                "target_role": row.target_role,
                "target_role_label": TARGET_ROLE_LABEL.get(row.target_role, row.target_role),
                "enabled": row.enabled,
                "last_delivered_message_id": (
                    item.last_delivered_message_id if item is not None else 0
                ),
                "backfill_status": item.backfill_status if item is not None else "idle",
            }
        )

    a_config = load_a_config(route.a_config)
    b_config = load_b_config(route.b_config)

    return {
        "id": route.id,
        "name": route.name,
        "business_type": route.business_type,
        "enabled": route.enabled,
        "priority": route.priority,
        "delay_seconds": route.delay_seconds,
        "hourly_limit": route.hourly_limit,
        "daily_limit": route.daily_limit,
        "exec_account_id": route.exec_account_id,
        "notify_bot_id": route.notify_bot_id,
        "sender_mode": route.sender_mode,
        "created_by": route.created_by,
        "source": (
            {
                "chat_id": source.id,
                "title": source.title,
                "display_name": source.display_name,
                "name": (
                    source.display_name or source.title or source.username or f"#{source.tg_id}"
                ),
                "username": source.username,
                "chat_type": source.chat_type,
            }
            if source is not None
            else None
        ),
        "targets": targets,
        "a_config": a_config.model_dump(mode="json"),
        "b_config": b_config.model_dump(mode="json"),
        "bundle_id": route.bundle_id,
        "source_chat_ids": await route_service.bundle_source_ids(session, route),
        "warnings": warnings,
        "created_at": as_utc(route.created_at),
        "updated_at": as_utc(route.updated_at),
    }


async def serialize_route_group(session: AsyncSession, routes: list[Route]) -> dict[str, Any]:
    """一条「逻辑线路」：多源线路的所有行聚成一条记录。

    代表行（第一条）提供配置与 id —— 编辑 / 删除 / 启停都按**整条线路**处理；
    监听源与接收目标在这里汇总：源按行列出，目标按 chat_id 去重。
    """
    payload = await serialize_route(session, routes[0])
    sources: list[dict[str, Any]] = []
    targets: dict[int, dict[str, Any]] = {}
    warnings: list[str] = []

    for route in routes:
        source = await chat_service.get_chat(session, route.source_chat_id)
        if source is not None:
            sources.append(
                {
                    "chat_id": source.id,
                    "name": (
                        source.display_name or source.title or source.username or f"#{source.tg_id}"
                    ),
                    "username": source.username,
                    "chat_type": source.chat_type,
                    "enabled": route.enabled,
                }
            )
        progress = {
            item.target_chat_id: item
            for item in await route_service.list_progress(session, route.id)
        }
        for row in await route_service.list_route_targets(session, route.id):
            chat = await chat_service.get_chat(session, row.target_chat_id)
            if chat is None:
                continue
            item = targets.get(row.target_chat_id)
            if item is None:
                row_progress = progress.get(row.target_chat_id)
                if chat.can_post is False:
                    warnings.append(f"接收目标「{chat.title or chat.tg_id}」没有发帖权限")
                item = {
                    "chat_id": chat.id,
                    "title": chat.title,
                    "display_name": chat.display_name,
                    "name": chat.display_name or chat.title or chat.username or f"#{chat.tg_id}",
                    "username": chat.username,
                    "chat_type": chat.chat_type,
                    "is_private": chat.is_private,
                    "can_post": chat.can_post,
                    "target_role": row.target_role,
                    "target_role_label": TARGET_ROLE_LABEL.get(row.target_role, row.target_role),
                    "enabled": row.enabled,
                    # 这条目标挂在了几个源上；小于线路源数说明只在部分源上生效
                    "source_count": 1,
                    # 水位线是按「源 + 目标」记的，这里给代表行的值（列表不展示，编辑器按单行读）
                    "last_delivered_message_id": (
                        row_progress.last_delivered_message_id if row_progress else 0
                    ),
                    "backfill_status": (row_progress.backfill_status if row_progress else "idle"),
                }
                targets[row.target_chat_id] = item
            else:
                item["enabled"] = bool(item["enabled"] and row.enabled)
                item["source_count"] += 1

    enabled_flags = [bool(route.enabled) for route in routes]
    payload.update(
        {
            "route_ids": [route.id for route in routes],
            "bundle_size": len(routes),
            "sources": sources,
            "source_count": len(sources),
            "targets": list(targets.values()),
            "target_count": len(targets),
            "enabled": all(enabled_flags),
            "mixed_enabled": any(enabled_flags) and not all(enabled_flags),
            "warnings": list(dict.fromkeys(warnings)),
        }
    )
    return payload


@router.get("")
async def list_routes(
    business_type: str | None = Query(default=None),
    enabled: bool | None = Query(default=None),
    source_chat_id: int | None = Query(default=None),
    keyword: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """线路列表：按「逻辑线路」分组，多源线路只占一行。"""
    groups, total = await route_service.list_route_groups(
        session,
        business_type=business_type,
        enabled=enabled,
        source_chat_id=source_chat_id,
        keyword=keyword,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [await serialize_route_group(session, group) for group in groups],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("", status_code=201)
async def create_route(
    payload: RouteCreateRequest,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """创建线路（支持多选监听源：一个源一条线路，共用一组配置）。"""
    source_ids = payload.source_chat_ids or (
        [payload.source_chat_id] if payload.source_chat_id else []
    )
    if not source_ids:
        raise ValidationFailedError("至少要选一个监听源")
    routes = await route_service.create_route_bundle(
        session,
        name=payload.name,
        source_chat_ids=source_ids,
        business_type=payload.business_type,
        target_chat_ids=payload.target_chat_ids,
        exec_account_id=payload.exec_account_id,
        notify_bot_id=payload.notify_bot_id,
        sender_mode=payload.sender_mode,
        priority=payload.priority,
        delay_seconds=payload.delay_seconds,
        hourly_limit=payload.hourly_limit,
        daily_limit=payload.daily_limit,
        enabled=payload.enabled,
        created_by=str(identity.get("username")),
        a_config=payload.a_config,
        b_config=payload.b_config,
    )
    detail = await serialize_route(session, routes[0])
    detail["created"] = len(routes)
    return detail


@router.post("/matrix", status_code=201)
async def create_matrix(
    payload: RouteMatrixRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """矩阵建线：源 × 目标，已存在的自动跳过。"""
    created, skipped = await route_service.create_routes_matrix(
        session,
        source_chat_ids=payload.source_chat_ids,
        target_chat_ids=payload.target_chat_ids,
        business_type=payload.business_type,
        exec_account_id=payload.exec_account_id,
        notify_bot_id=payload.notify_bot_id,
        sender_mode=payload.sender_mode,
        delay_seconds=payload.delay_seconds,
        a_config=payload.a_config,
        b_config=payload.b_config,
    )
    return {"created": len(created), "skipped": skipped}


@router.get("/{route_id}")
async def get_route_detail(
    route_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """线路详情。"""
    route = await route_service.get_route(session, route_id)
    if route is None:
        raise NotFoundError("线路不存在")
    await route_service.ensure_progress(session, route_id)
    return await serialize_route(session, route)


@router.patch("/{route_id}")
async def update_route(
    route_id: int,
    payload: RouteUpdateRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """更新线路配置；带 source_chat_ids 时会同步整条多源线路。"""
    fields = {
        "name": payload.name,
        "business_type": payload.business_type,
        "exec_account_id": payload.exec_account_id,
        "notify_bot_id": payload.notify_bot_id,
        "sender_mode": payload.sender_mode,
        "priority": payload.priority,
        "delay_seconds": payload.delay_seconds,
        "hourly_limit": payload.hourly_limit,
        "daily_limit": payload.daily_limit,
        "enabled": payload.enabled,
        "a_config": payload.a_config,
        "b_config": payload.b_config,
    }
    route = await route_service.update_route(session, route_id, **fields)
    siblings = await route_service.bundle_routes(session, route)
    if payload.source_chat_ids:
        await route_service.sync_route_bundle_sources(
            session,
            route,
            payload.source_chat_ids,
        )
        siblings = await route_service.bundle_routes(session, route)
    # 列表页的整组启停 / 编辑器改了配置：同组的其他行一起改（多源时它们是同一套规则）
    if payload.source_chat_ids or payload.apply_to_bundle:
        for sibling in siblings:
            if sibling.id != route_id:
                await route_service.update_route(session, sibling.id, **fields)
    route = await route_service.get_route(session, route_id)
    return await serialize_route(session, route)


@router.delete("/{route_id}")
async def delete_route(
    route_id: int,
    bundle: bool = Query(default=True, description="多源线路是否整条删除"),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """删除线路（多源线路默认整条一起删）。"""
    if bundle:
        result = await route_service.delete_route_bundle(session, route_id)
        return {"id": route_id, "deleted": True, **result}
    route = await route_service.delete_route(session, route_id)
    return {"id": route_id, "name": route.name, "deleted": True, "deleted_count": 1}


@router.post("/{route_id}/targets", status_code=201)
async def add_targets(
    route_id: int,
    payload: RouteTargetAddRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """给线路追加接收目标（多源线路：整条一起加，避免只对部分源生效）。"""
    route = await route_service.get_route(session, route_id)
    if route is None:
        raise NotFoundError("线路不存在")
    added: list[Any] = []
    skipped: list[Any] = []
    for item in await route_service.bundle_routes(session, route):
        rows, missed = await route_service.add_targets(session, item.id, payload.chat_ids)
        added.extend(rows)
        skipped.extend(missed)
    return {
        "added": list(dict.fromkeys(item.target_chat_id for item in added)),
        "skipped": list(dict.fromkeys(skipped)),
    }


@router.patch("/{route_id}/targets/{chat_id}")
async def set_target_enabled(
    route_id: int,
    chat_id: int,
    payload: EnabledUpdate,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """接收目标的启停（多源线路：整条一起改）。"""
    route = await route_service.get_route(session, route_id)
    if route is None:
        raise NotFoundError("线路不存在")
    for item in await route_service.bundle_routes(session, route):
        await route_service.set_target_enabled(session, item.id, chat_id, payload.enabled)
    return {"chat_id": chat_id, "enabled": payload.enabled}


@router.delete("/{route_id}/targets/{chat_id}")
async def remove_target(
    route_id: int,
    chat_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """移除线路的接收目标（多源线路：整条一起移除）。"""
    route = await route_service.get_route(session, route_id)
    if route is None:
        raise NotFoundError("线路不存在")
    for item in await route_service.bundle_routes(session, route):
        await route_service.remove_target(session, item.id, chat_id)
    return {"route_id": route_id, "chat_id": chat_id, "removed": True}


@router.get("/{route_id}/progress")
async def list_progress(
    route_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """线路各目标的搬运进度。"""
    rows = await route_service.ensure_progress(session, route_id)
    return {
        "items": [
            {
                "target_chat_id": item.target_chat_id,
                "last_delivered_message_id": item.last_delivered_message_id,
                "backfill_status": item.backfill_status,
                "backfill_error": item.backfill_error,
                "last_run_at": as_utc(item.last_run_at),
            }
            for item in rows
        ]
    }


@router.post("/{route_id}/targets/{chat_id}/reset")
async def reset_progress(
    route_id: int,
    chat_id: int,
    payload: ResetProgressRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """重置某个目标的搬运进度（需输入 RESET 确认；多源线路整条一起重置）。"""
    route = await route_service.get_route(session, route_id)
    if route is None:
        raise NotFoundError("线路不存在")
    progress = None
    for item in await route_service.bundle_routes(session, route):
        progress = await route_service.reset_target_progress(
            session,
            item.id,
            chat_id,
            confirm=payload.confirm,
        )
    return {
        "route_id": route_id,
        "target_chat_id": chat_id,
        "last_delivered_message_id": progress.last_delivered_message_id if progress else 0,
        "reset": True,
    }
