"""线路服务：CRUD、矩阵建线、目标管理与目标级水位线。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationFailedError
from app.core.route_config import (
    ACarryConfig,
    BMonitorConfig,
    dump_config,
    parse_a_config_payload,
    parse_b_config_payload,
    validate_route_config,
)
from app.db.models import (
    BACKFILL_IDLE,
    BUSINESS_CARRY,
    BUSINESS_TYPES,
    TARGET_ROLE_CONTENT,
    Chat,
    ControlBot,
    Route,
    RouteTarget,
    RouteTargetProgress,
    TgAccount,
)

DEFAULT_TARGET_ROLE = TARGET_ROLE_CONTENT


async def list_routes(
    session: AsyncSession,
    *,
    business_type: str | None = None,
    enabled: bool | None = None,
    source_chat_id: int | None = None,
    keyword: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[Route], int]:
    """分页查询线路。"""
    conditions = []
    if business_type:
        conditions.append(Route.business_type == business_type)
    if enabled is not None:
        conditions.append(Route.enabled.is_(enabled))
    if source_chat_id is not None:
        conditions.append(Route.source_chat_id == source_chat_id)
    if keyword:
        conditions.append(Route.name.like(f"%{keyword}%"))

    statement = select(Route).order_by(Route.priority, Route.id)
    count_statement = select(func.count()).select_from(Route)
    for condition in conditions:
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)
    rows = list(await session.scalars(statement.limit(limit).offset(offset)))
    total = int(await session.scalar(count_statement) or 0)
    return rows, total


async def get_route(session: AsyncSession, route_id: int) -> Route | None:
    """按 ID 查询线路。"""
    return await session.get(Route, route_id)


async def list_route_targets(session: AsyncSession, route_id: int) -> list[RouteTarget]:
    """线路的接收目标。"""
    return list(
        await session.scalars(
            select(RouteTarget).where(RouteTarget.route_id == route_id).order_by(RouteTarget.id)
        )
    )


async def find_existing_route(
    session: AsyncSession,
    *,
    source_chat_id: int,
    business_type: str,
    target_chat_id: int,
) -> Route | None:
    """按"源 + 业务类型 + 目标"找已存在的线路（矩阵建线去重用）。"""
    statement = (
        select(Route)
        .join(RouteTarget, RouteTarget.route_id == Route.id)
        .where(
            Route.source_chat_id == source_chat_id,
            Route.business_type == business_type,
            RouteTarget.target_chat_id == target_chat_id,
        )
        .limit(1)
    )
    return await session.scalar(statement)


async def create_route(
    session: AsyncSession,
    *,
    name: str,
    source_chat_id: int,
    business_type: str,
    target_chat_ids: list[int],
    exec_account_id: int | None = None,
    notify_bot_id: int | None = None,
    priority: int = 100,
    delay_seconds: float = 1.0,
    hourly_limit: int | None = None,
    daily_limit: int | None = None,
    enabled: bool = True,
    created_by: str | None = None,
    a_config: dict[str, Any] | None = None,
    b_config: dict[str, Any] | None = None,
) -> Route:
    """创建线路（含接收目标与水印线初始化）。"""
    source = await _require_source(session, source_chat_id)
    targets = await _require_targets(session, target_chat_ids)
    a_model: ACarryConfig = parse_a_config_payload(a_config)
    b_model: BMonitorConfig = parse_b_config_payload(b_config)
    validate_route_config(business_type, a_model, b_model)
    await _validate_owner_refs(session, exec_account_id, notify_bot_id)

    route = Route(
        name=(name or "").strip() or _default_name(source, targets[0]),
        source_chat_id=source_chat_id,
        business_type=business_type,
        exec_account_id=exec_account_id,
        notify_bot_id=notify_bot_id,
        priority=priority,
        delay_seconds=delay_seconds,
        hourly_limit=hourly_limit,
        daily_limit=daily_limit,
        enabled=enabled,
        created_by=created_by,
        a_config=dump_config(a_model) if business_type == BUSINESS_CARRY else dump_config(a_model),
        b_config=dump_config(b_model),
    )
    session.add(route)
    await session.flush()
    for chat in targets:
        await _add_target_row(session, route.id, chat)
    await session.commit()
    await session.refresh(route)
    return route


async def create_routes_matrix(
    session: AsyncSession,
    *,
    source_chat_ids: list[int],
    target_chat_ids: list[int],
    business_type: str,
    exec_account_id: int | None = None,
    notify_bot_id: int | None = None,
    delay_seconds: float = 1.0,
    a_config: dict[str, Any] | None = None,
    b_config: dict[str, Any] | None = None,
) -> tuple[list[Route], list[dict[str, int]]]:
    """矩阵建线：每个「源 × 目标」建一条线路，已存在的跳过。"""
    if business_type not in BUSINESS_TYPES:
        raise ValidationFailedError(f"业务类型必须是 {'/'.join(BUSINESS_TYPES)} 之一")
    if not source_chat_ids:
        raise ValidationFailedError("请至少选择一个监听源")
    if not target_chat_ids:
        raise ValidationFailedError("请至少选择一个接收目标")

    sources = await _require_sources(session, source_chat_ids)
    targets = await _require_targets(session, target_chat_ids)

    created: list[Route] = []
    skipped: list[dict[str, int]] = []
    for source in sources:
        for target in targets:
            existing = await find_existing_route(
                session,
                source_chat_id=source.id,
                business_type=business_type,
                target_chat_id=target.id,
            )
            if existing is not None:
                skipped.append({"source_chat_id": source.id, "target_chat_id": target.id})
                continue
            route = await create_route(
                session,
                name=_default_name(source, target),
                source_chat_id=source.id,
                business_type=business_type,
                target_chat_ids=[target.id],
                exec_account_id=exec_account_id,
                notify_bot_id=notify_bot_id,
                delay_seconds=delay_seconds,
                a_config=a_config,
                b_config=b_config,
            )
            created.append(route)
    return created, skipped


async def update_route(
    session: AsyncSession,
    route_id: int,
    *,
    name: str | None = None,
    business_type: str | None = None,
    exec_account_id: int | None = None,
    notify_bot_id: int | None = None,
    priority: int | None = None,
    delay_seconds: float | None = None,
    hourly_limit: int | None = None,
    daily_limit: int | None = None,
    enabled: bool | None = None,
    a_config: dict[str, Any] | None = None,
    b_config: dict[str, Any] | None = None,
) -> Route:
    """更新线路配置。"""
    route = await get_route(session, route_id)
    if route is None:
        raise NotFoundError("线路不存在")

    next_type = business_type or route.business_type
    a_model = (
        parse_a_config_payload(a_config)
        if a_config is not None
        else parse_a_config_payload(_decode(route.a_config))
    )
    b_model = (
        parse_b_config_payload(b_config)
        if b_config is not None
        else parse_b_config_payload(_decode(route.b_config))
    )
    validate_route_config(next_type, a_model, b_model)
    await _validate_owner_refs(session, exec_account_id, notify_bot_id)

    if name is not None:
        alias = name.strip()
        if not alias:
            raise ValidationFailedError("线路名不能为空")
        route.name = alias
    route.business_type = next_type
    if exec_account_id is not None:
        route.exec_account_id = exec_account_id
    if notify_bot_id is not None:
        route.notify_bot_id = notify_bot_id
    if priority is not None:
        route.priority = priority
    if delay_seconds is not None:
        route.delay_seconds = delay_seconds
    if hourly_limit is not None:
        route.hourly_limit = hourly_limit
    if daily_limit is not None:
        route.daily_limit = daily_limit
    if enabled is not None:
        route.enabled = enabled
    route.a_config = dump_config(a_model)
    route.b_config = dump_config(b_model)

    await session.commit()
    await session.refresh(route)
    return route


async def delete_route(session: AsyncSession, route_id: int) -> Route:
    """删除线路（目标与水位线级联删除）。"""
    route = await get_route(session, route_id)
    if route is None:
        raise NotFoundError("线路不存在")
    await session.delete(route)
    await session.commit()
    return route


async def add_targets(
    session: AsyncSession,
    route_id: int,
    chat_ids: list[int],
) -> tuple[list[RouteTarget], list[int]]:
    """给线路追加接收目标（新增目标只补自己缺的历史）。"""
    route = await get_route(session, route_id)
    if route is None:
        raise NotFoundError("线路不存在")
    chats = await _require_targets(session, chat_ids)
    existing = {item.target_chat_id for item in await list_route_targets(session, route_id)}

    added: list[RouteTarget] = []
    for chat in chats:
        if chat.id in existing:
            continue
        added.append(await _add_target_row(session, route_id, chat))
    await session.commit()
    return added, [chat.id for chat in chats if chat.id in existing]


async def remove_target(session: AsyncSession, route_id: int, chat_id: int) -> None:
    """移除线路的某个接收目标。"""
    row = await _get_target_row(session, route_id, chat_id)
    if row is None:
        raise NotFoundError("该线路没有这个接收目标")
    await session.delete(row)
    progress = await session.get(RouteTargetProgress, (route_id, chat_id))
    if progress is not None:
        await session.delete(progress)
    await session.commit()


async def set_target_enabled(
    session: AsyncSession,
    route_id: int,
    chat_id: int,
    enabled: bool,
) -> RouteTarget:
    """单个目标的启停。"""
    row = await _get_target_row(session, route_id, chat_id)
    if row is None:
        raise NotFoundError("该线路没有这个接收目标")
    row.enabled = enabled
    await session.commit()
    await session.refresh(row)
    return row


async def set_chat_target_enabled_everywhere(
    session: AsyncSession,
    chat_id: int,
    enabled: bool,
) -> int:
    """接收组总开关：一次性启停这个群在所有线路上的投递。

    界面上「接收组」页的开关是总开关；线路抽屉里的开关才是单条线路的开关。
    这里把它们同步，避免出现「界面显示关闭、实际还在收」的错觉。
    """
    rows = list(
        await session.scalars(select(RouteTarget).where(RouteTarget.target_chat_id == chat_id))
    )
    changed = 0
    for row in rows:
        if row.enabled != enabled:
            row.enabled = enabled
            changed += 1
    await session.commit()
    return changed


async def ensure_progress(
    session: AsyncSession,
    route_id: int,
) -> list[RouteTargetProgress]:
    """确保每个启用目标都有水位线记录。"""
    rows = await list_route_targets(session, route_id)
    existing = {
        item.target_chat_id
        for item in await session.scalars(
            select(RouteTargetProgress).where(RouteTargetProgress.route_id == route_id)
        )
    }
    created: list[RouteTargetProgress] = []
    for row in rows:
        if row.target_chat_id in existing:
            continue
        progress = RouteTargetProgress(
            route_id=route_id,
            target_chat_id=row.target_chat_id,
            backfill_status=BACKFILL_IDLE,
        )
        session.add(progress)
        created.append(progress)
    if created:
        await session.commit()
    return list(
        await session.scalars(
            select(RouteTargetProgress).where(RouteTargetProgress.route_id == route_id)
        )
    )


async def list_progress(
    session: AsyncSession,
    route_id: int,
) -> list[RouteTargetProgress]:
    """线路各目标的水位线。"""
    return list(
        await session.scalars(
            select(RouteTargetProgress).where(RouteTargetProgress.route_id == route_id)
        )
    )


async def reset_target_progress(
    session: AsyncSession,
    route_id: int,
    chat_id: int,
    *,
    confirm: str,
) -> RouteTargetProgress:
    """重置某个目标的搬运进度（危险操作，必须输入 RESET）。"""
    if (confirm or "").strip().upper() != "RESET":
        raise ValidationFailedError("该操作会重搬历史消息，请输入 RESET 确认")
    if await _get_target_row(session, route_id, chat_id) is None:
        raise NotFoundError("该线路没有这个接收目标")

    progress = await session.get(RouteTargetProgress, (route_id, chat_id))
    if progress is None:
        progress = RouteTargetProgress(
            route_id=route_id,
            target_chat_id=chat_id,
            backfill_status=BACKFILL_IDLE,
        )
        session.add(progress)
    progress.last_delivered_message_id = 0
    progress.backfill_status = BACKFILL_IDLE
    progress.backfill_error = None
    await session.commit()
    await session.refresh(progress)
    return progress


async def _add_target_row(
    session: AsyncSession,
    route_id: int,
    chat: Chat,
) -> RouteTarget:
    row = RouteTarget(
        route_id=route_id,
        target_chat_id=chat.id,
        target_role=chat.target_role or DEFAULT_TARGET_ROLE,
        enabled=True,
    )
    session.add(row)
    session.add(
        RouteTargetProgress(
            route_id=route_id,
            target_chat_id=chat.id,
            backfill_status=BACKFILL_IDLE,
        )
    )
    await session.flush()
    return row


async def _get_target_row(
    session: AsyncSession,
    route_id: int,
    chat_id: int,
) -> RouteTarget | None:
    return await session.scalar(
        select(RouteTarget).where(
            RouteTarget.route_id == route_id,
            RouteTarget.target_chat_id == chat_id,
        )
    )


async def _require_source(session: AsyncSession, chat_id: int) -> Chat:
    chat = await session.get(Chat, chat_id)
    if chat is None:
        raise NotFoundError("监听源不存在")
    if not chat.is_source:
        raise ValidationFailedError("该群组还不是监听源，请先在「监听源」页添加")
    return chat


async def _require_sources(session: AsyncSession, chat_ids: list[int]) -> list[Chat]:
    chats: list[Chat] = []
    for chat_id in chat_ids:
        chats.append(await _require_source(session, chat_id))
    return chats


async def _require_targets(session: AsyncSession, chat_ids: list[int]) -> list[Chat]:
    if not chat_ids:
        raise ValidationFailedError("请至少选择一个接收目标")
    chats: list[Chat] = []
    for chat_id in chat_ids:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            raise NotFoundError("接收目标不存在")
        if not chat.is_target:
            raise ValidationFailedError("该群组还不是接收组，请先在「接收组」页添加")
        chats.append(chat)
    return chats


async def _validate_owner_refs(
    session: AsyncSession,
    exec_account_id: int | None,
    notify_bot_id: int | None,
) -> None:
    if exec_account_id is not None and await session.get(TgAccount, exec_account_id) is None:
        raise NotFoundError("执行账号不存在")
    if notify_bot_id is not None and await session.get(ControlBot, notify_bot_id) is None:
        raise NotFoundError("控制 Bot 不存在")


def _decode(raw: str | None) -> dict[str, Any]:
    import json

    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _default_name(source: Chat, target: Chat) -> str:
    source_name = source.title or source.username or f"源{source.id}"
    target_name = target.title or target.username or f"目标{target.id}"
    return f"{source_name} → {target_name}"
