"""从 Telegram 同步聊天对象，并把链接输入变成可用的源/接收组。"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account_client_pool import POOL, USE_POOL
from app.core.config import AppConfig
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.source_resolver import ResolvedTarget, resolve_target
from app.core.telegram_client import (
    ChatProfile,
    check_can_post,
    connect_user_client,
    fetch_chat_profile,
    fetch_dialogs,
    fetch_migrations,
    join_invite,
    profile_from_entity,
    session_file_path,
)
from app.db.models import TenantChat, TgAccount
from app.services import chat_service, tg_account_service


async def _open_client(
    config: AppConfig,
    session: AsyncSession,
    *,
    account_id: int | None,
    client_factory: Any,
) -> tuple[TgAccount, Any]:
    """取执行账号并打开客户端。"""
    account = (
        await tg_account_service.get_account(session, account_id)
        if account_id is not None
        else await tg_account_service.get_default_account(session)
    )
    if account is None:
        raise NotFoundError("还没有可用的执行账号，请先在「执行账号池」登记并登录")

    phone, api_id, api_hash = tg_account_service.decrypt_credentials(config, account)
    session_path = session_file_path(config, account.session_name)
    factory = client_factory or _default_client_factory
    client = await factory(config, api_id=api_id, api_hash=api_hash, session_path=session_path)
    return account, client


async def _default_client_factory(
    config: AppConfig,
    *,
    api_id: int,
    api_hash: str,
    session_path: Any,
) -> Any:
    return await connect_user_client(
        config,
        api_id=api_id,
        api_hash=api_hash,
        session_path=session_path,
    )


@asynccontextmanager
async def account_client(
    session: AsyncSession,
    config: AppConfig,
    *,
    account_id: int | None = None,
    client_factory: Any = None,
) -> AsyncIterator[tuple[TgAccount, Any]]:
    """借一条执行账号连接。

    - 生产（没注入 factory）：从连接池取，退出时**不**断开，下次直接复用——
      交互动作（加盟 / 刷新 / 同步群组池）不用每次等 8 秒握手。
    - 测试（注入 factory）：照旧新建、用完断开，替身不进池。
    """
    account = (
        await tg_account_service.get_account(session, account_id)
        if account_id is not None
        else await tg_account_service.get_default_account(session)
    )
    if account is None:
        raise NotFoundError("还没有可用的执行账号，请先在「执行账号池」登记并登录")

    pooled = client_factory is None and USE_POOL
    if pooled:

        async def opener() -> Any:
            _row, client = await _open_client(
                config,
                session,
                account_id=account.id,
                client_factory=None,
            )
            return client

        client = await POOL.get(account.id, opener)
    else:
        _row, client = await _open_client(
            config,
            session,
            account_id=account.id,
            client_factory=client_factory,
        )
    try:
        yield account, client
    finally:
        if not pooled:
            with contextlib.suppress(Exception):
                await client.disconnect()


@asynccontextmanager
async def account_client_optional(
    session: AsyncSession,
    config: AppConfig,
    *,
    account_id: int | None = None,
    client_factory: Any = None,
) -> AsyncIterator[tuple[TgAccount | None, Any]]:
    """拿不到可用账号时给 ``(None, None)``，由调用方决定怎么降级。

    用在「Telegram 只是其中一条渠道」的场景（在线补搜）：没有账号时
    其它渠道照常出结果，而不是整条请求失败。
    """
    try:
        lease = account_client(
            session,
            config,
            account_id=account_id,
            client_factory=client_factory,
        )
        account, client = await lease.__aenter__()
    except Exception:  # noqa: BLE001 - 没账号 / 连不上都降级
        yield None, None
        return
    try:
        yield account, client
    finally:
        with contextlib.suppress(Exception):
            await lease.__aexit__(None, None, None)


async def sync_dialogs(
    session: AsyncSession,
    config: AppConfig,
    *,
    account_id: int | None = None,
    client_factory: Any = None,
    limit: int = 500,
) -> dict[str, Any]:
    """把执行账号已加入的群组/频道同步到本地群组池。"""
    async with account_client(
        session,
        config,
        account_id=account_id,
        client_factory=client_factory,
    ) as (account, client):
        migrations = await fetch_migrations(client, limit=limit)
        profiles = await fetch_dialogs(client, limit=limit)

    created = 0
    updated = 0
    migrated = 0
    # 群升级成超级群：先把本地记录迁到新 id，再走常规 upsert
    for old_tg_id, entity in migrations:
        moved = await chat_service.migrate_chat(
            session,
            old_tg_id=old_tg_id,
            profile=profile_from_entity(entity),
        )
        if moved is not None:
            migrated += 1
    for profile in profiles:
        if profile.tg_id <= 0:
            continue
        existing = await chat_service.get_chat_by_tg_id(session, profile.tg_id)
        await chat_service.upsert_chat_from_profile(session, profile, joined=True)
        if existing is None:
            created += 1
        else:
            updated += 1

    await tg_account_service.touch_account(session, account)
    return {
        "account": account.name,
        "fetched": len(profiles),
        "created": created,
        "updated": updated,
        "migrated": migrated,
    }


async def ensure_chat_from_input(
    session: AsyncSession,
    config: AppConfig,
    *,
    raw_input: str,
    join: bool = False,
    account_id: int | None = None,
    client_factory: Any = None,
) -> TenantChat:
    """把用户粘贴的链接/标识解析并登记到群组池，返回聊天对象。"""
    try:
        target = resolve_target(raw_input)
    except ValueError as exc:
        raise ValidationFailedError(str(exc)) from exc

    if target.kind == "phone":
        raise ValidationFailedError("这里需要群组或频道链接，不是手机号")
    if target.kind == "invite" and not join:
        raise ValidationFailedError("私有邀请链接需要勾选「允许执行账号加入」，否则无法识别该群组")

    async with account_client(
        session,
        config,
        account_id=account_id,
        client_factory=client_factory,
    ) as (account, client):
        profile = await _profile_for_target(client, target, join=join)

    if profile.tg_id <= 0:
        raise ValidationFailedError(f"无法解析该链接对应的群组：{raw_input}")

    chat = await chat_service.upsert_chat_from_profile(session, profile, joined=True)
    await tg_account_service.touch_account(session, account)
    return chat


async def _profile_for_target(
    client: Any,
    target: ResolvedTarget,
    *,
    join: bool,
) -> ChatProfile:
    if target.kind == "invite":
        return await join_invite(client, target.value)
    return await fetch_chat_profile(client, target)


async def check_targets_access(
    session: AsyncSession,
    config: AppConfig,
    chats: list[TenantChat],
    *,
    client_factory: Any = None,
    account_id: int | None = None,
) -> dict[int, bool | None]:
    """批量权限预检：返回 `{本地 chat_id: 是否有发言权限}`（失败返回 None）。"""
    if not chats:
        return {}
    try:
        async with account_client(
            session,
            config,
            account_id=account_id,
            client_factory=client_factory,
        ) as (_account, client):
            results: dict[int, bool | None] = {}
            for chat in chats:
                results[chat.id] = await check_can_post(client, chat.tg_id)
            return results
    except Exception:  # noqa: BLE001 - 预检失败不阻断添加流程
        return {chat.id: None for chat in chats}


__all__ = [
    "account_client",
    "check_targets_access",
    "ensure_chat_from_input",
    "sync_dialogs",
]
