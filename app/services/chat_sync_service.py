"""从 Telegram 同步聊天对象，并把链接输入变成可用的源/接收组。"""

from __future__ import annotations

import contextlib
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.source_resolver import ResolvedTarget, resolve_target
from app.core.telegram_client import (
    ChatProfile,
    check_can_post,
    connect_user_client,
    fetch_chat_profile,
    fetch_dialogs,
    join_invite,
    session_file_path,
)
from app.db.models import Chat, TgAccount
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


async def sync_dialogs(
    session: AsyncSession,
    config: AppConfig,
    *,
    account_id: int | None = None,
    client_factory: Any = None,
    limit: int = 500,
) -> dict[str, Any]:
    """把执行账号已加入的群组/频道同步到本地群组池。"""
    account, client = await _open_client(
        config,
        session,
        account_id=account_id,
        client_factory=client_factory,
    )
    try:
        profiles = await fetch_dialogs(client, limit=limit)
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()

    created = 0
    updated = 0
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
    }


async def ensure_chat_from_input(
    session: AsyncSession,
    config: AppConfig,
    *,
    raw_input: str,
    join: bool = False,
    account_id: int | None = None,
    client_factory: Any = None,
) -> Chat:
    """把用户粘贴的链接/标识解析并登记到群组池，返回聊天对象。"""
    try:
        target = resolve_target(raw_input)
    except ValueError as exc:
        raise ValidationFailedError(str(exc)) from exc

    if target.kind == "phone":
        raise ValidationFailedError("这里需要群组或频道链接，不是手机号")
    if target.kind == "invite" and not join:
        raise ValidationFailedError("私有邀请链接需要勾选「允许执行账号加入」，否则无法识别该群组")

    account, client = await _open_client(
        config,
        session,
        account_id=account_id,
        client_factory=client_factory,
    )
    try:
        profile = await _profile_for_target(client, target, join=join)
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()

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
    chats: list[Chat],
    *,
    client_factory: Any = None,
    account_id: int | None = None,
) -> dict[int, bool | None]:
    """批量权限预检：返回 `{本地 chat_id: 是否有发言权限}`（失败返回 None）。"""
    if not chats:
        return {}
    try:
        _account, client = await _open_client(
            config,
            session,
            account_id=account_id,
            client_factory=client_factory,
        )
    except Exception:  # noqa: BLE001 - 预检失败不阻断添加流程
        return {chat.id: None for chat in chats}

    results: dict[int, bool | None] = {}
    try:
        for chat in chats:
            results[chat.id] = await check_can_post(client, chat.tg_id)
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
    return results


__all__ = ["check_targets_access", "ensure_chat_from_input", "sync_dialogs"]
