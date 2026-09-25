"""历史补齐：按目标级水位线拉取源消息并入队。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.content_cleaner import (
    CleanRules,
    filter_reason,
    resolve_caption,
)
from app.core.route_config import ACarryConfig
from app.core.telegram_client import (
    iter_source_messages,
    message_view_from_telethon,
    resolve_entity,
)
from app.db.models import Chat, Route, RouteTarget, RouteTargetProgress
from app.services import delivery_service


async def route_target_chat_ids(
    session: AsyncSession,
    route_id: int,
    *,
    only_enabled: bool = True,
) -> list[int]:
    """线路的接收目标（默认只看启用中的）。"""
    statement = select(RouteTarget).where(RouteTarget.route_id == route_id)
    if only_enabled:
        statement = statement.where(RouteTarget.enabled.is_(True))
    rows = await session.scalars(statement.order_by(RouteTarget.id))
    return [row.target_chat_id for row in rows]


async def watermark_map(
    session: AsyncSession,
    route_id: int,
    target_chat_ids: list[int],
) -> dict[int, int]:
    """目标级水位线：`{target_chat_id: 已投递到的最大消息 ID}`。"""
    result = {chat_id: 0 for chat_id in target_chat_ids}
    rows = await session.scalars(
        select(RouteTargetProgress).where(RouteTargetProgress.route_id == route_id)
    )
    for row in rows:
        if row.target_chat_id in result:
            result[row.target_chat_id] = int(row.last_delivered_message_id or 0)
    return result


async def sync_route_history(
    session: AsyncSession,
    config: AppConfig,
    *,
    route: Route,
    client: Any,
    a_config: ACarryConfig,
    source_entity: Any = None,
    apply_filter: bool = True,
) -> dict[str, Any]:
    """为一条 A 线补齐历史：只补每个目标缺失的部分。"""
    target_ids = await route_target_chat_ids(session, route.id)
    if not target_ids:
        return {"route": route.name, "inspected": 0, "enqueued": 0, "targets": 0}

    watermarks = await watermark_map(session, route.id, target_ids)
    start_from = min(watermarks.values())
    messages = await iter_source_messages(
        client,
        source_entity,
        min_id=start_from,
        limit=a_config.history_limit,
        skip_pinned=a_config.skip_pinned,
    )

    rules = CleanRules.from_config(a_config.model_dump())
    inspected = 0
    enqueued = 0
    filtered = 0
    advanced: dict[int, int] = {}

    for message in messages:
        view = message_view_from_telethon(message)
        if view.message_id <= start_from:
            continue
        inspected += 1

        if apply_filter:
            reason = filter_reason(view, content_types=a_config.content_types)
            if reason is not None:
                filtered += 1
                continue

        from app.core.content_cleaner import clean_text, is_empty_text

        caption = clean_text(view.text, rules)
        cleaned, keep = resolve_caption(
            caption,
            empty_text_policy=a_config.empty_text_policy,
            has_media=view.kind != "text",
        )
        if not keep:
            filtered += 1
            continue
        if is_empty_text(cleaned):
            cleaned = ""

        pending_targets = [
            chat_id for chat_id in target_ids if view.message_id > watermarks.get(chat_id, 0)
        ]
        if not pending_targets:
            continue

        created = await delivery_service.enqueue_message(
            session,
            route=route,
            target_chat_ids=pending_targets,
            source_chat_id=route.source_chat_id,
            source_message_id=view.message_id,
            media_group_id=view.grouped_id,
            source_message_ids=[view.message_id],
        )
        enqueued += len(created)
        for chat_id in pending_targets:
            advanced[chat_id] = max(advanced.get(chat_id, 0), view.message_id)

    # 入队即推进水位线：任务已持久化，重启后仍会继续投递
    for chat_id, message_id in advanced.items():
        await delivery_service.advance_progress(
            session,
            route_id=route.id,
            target_chat_id=chat_id,
            source_message_id=message_id,
        )

    return {
        "route": route.name,
        "inspected": inspected,
        "enqueued": enqueued,
        "filtered": filtered,
        "targets": len(target_ids),
    }


async def sync_all_routes(
    session: AsyncSession,
    config: AppConfig,
    *,
    client: Any,
    route_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """批量补齐：默认处理所有启用的 A 线。"""
    from app.core.route_config import load_a_config

    statement = select(Route).where(Route.business_type == "A", Route.enabled.is_(True))
    if route_ids:
        statement = statement.where(Route.id.in_(route_ids))
    routes = list(await session.scalars(statement.order_by(Route.priority, Route.id)))

    results: list[dict[str, Any]] = []
    for route in routes:
        source = await session.get(Chat, route.source_chat_id)
        if source is None or not source.tg_id:
            results.append({"route": route.name, "error": "监听源不存在"})
            continue
        try:
            source_entity = await resolve_entity(client, int(source.tg_id))
        except Exception as exc:  # noqa: BLE001 - 单个源失败不影响其他线路
            results.append({"route": route.name, "error": str(exc)})
            continue
        results.append(
            await sync_route_history(
                session,
                config,
                route=route,
                client=client,
                a_config=load_a_config(route.a_config),
                source_entity=source_entity,
            )
        )
    return results
