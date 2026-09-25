"""控制 Bot 服务：Token 校验、多 Bot 管理与默认切换。"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.errors import NotFoundError, UserExistsError, ValidationFailedError
from app.core.security import FieldCipher
from app.core.telegram_client import AccountProfile, build_bot_client, fetch_bot_profile
from app.db.models import ControlBot


def load_admin_ids(bot: ControlBot) -> list[int]:
    """读取管理员 ID 列表（容错）。"""
    try:
        value = json.loads(bot.admin_ids or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        with contextlib.suppress(TypeError, ValueError):
            result.append(int(item))
    return result


def dump_admin_ids(admin_ids: list[int] | None) -> str:
    """序列化管理员 ID 列表（去重、保序）。"""
    result: list[int] = []
    for item in admin_ids or []:
        value = int(item)
        if value not in result:
            result.append(value)
    return json.dumps(result)


async def _default_client_factory(config: AppConfig, token: str) -> Any:
    client = build_bot_client(config, token)
    await client.start(bot_token=token)
    return client


async def validate_bot_token(
    config: AppConfig,
    token: str,
    *,
    client_factory: Any = None,
) -> AccountProfile:
    """调用 getMe 校验 Bot Token，返回 Bot 资料。"""
    text = (token or "").strip()
    if ":" not in text or len(text) < 20:
        raise ValidationFailedError("Bot Token 格式不正确（形如 123456:ABC-DEF...）")

    factory = client_factory or _default_client_factory
    client = None
    try:
        client = await factory(config, text)
        return await fetch_bot_profile(client)
    except ValidationFailedError:
        raise
    except Exception as exc:  # noqa: BLE001 - 把 Telegram 侧错误统一成参数错误
        raise ValidationFailedError(f"Bot Token 校验失败：{exc}") from exc
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()


async def list_bots(
    session: AsyncSession,
    *,
    enabled: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ControlBot], int]:
    """分页查询控制 Bot。"""
    statement = select(ControlBot).order_by(ControlBot.id)
    count_statement = select(func.count()).select_from(ControlBot)
    if enabled is not None:
        statement = statement.where(ControlBot.enabled.is_(enabled))
        count_statement = count_statement.where(ControlBot.enabled.is_(enabled))
    rows = list(await session.scalars(statement.limit(limit).offset(offset)))
    total = int(await session.scalar(count_statement) or 0)
    return rows, total


async def get_bot(session: AsyncSession, bot_id: int) -> ControlBot | None:
    """按 ID 查询控制 Bot。"""
    return await session.get(ControlBot, bot_id)


async def get_bot_by_name(session: AsyncSession, name: str) -> ControlBot | None:
    """按名称查询控制 Bot。"""
    return await session.scalar(select(ControlBot).where(ControlBot.name == (name or "").strip()))


async def get_default_bot(session: AsyncSession) -> ControlBot | None:
    """返回默认控制 Bot。"""
    return await session.scalar(
        select(ControlBot).where(ControlBot.is_default.is_(True)).order_by(ControlBot.id)
    )


async def create_bot(
    session: AsyncSession,
    config: AppConfig,
    *,
    name: str,
    token: str,
    admin_ids: list[int] | None = None,
    is_default: bool = False,
    note: str | None = None,
    client_factory: Any = None,
) -> ControlBot:
    """绑定控制 Bot（保存前先校验 Token）。"""
    alias = (name or "").strip()
    if not alias:
        raise ValidationFailedError("请填写机器人名称")
    if await get_bot_by_name(session, alias) is not None:
        raise UserExistsError("该名称已存在")

    profile = await validate_bot_token(config, token, client_factory=client_factory)
    cipher = FieldCipher.from_config(config)

    bot = ControlBot(
        name=alias,
        bot_username=profile.username,
        bot_telegram_id=profile.tg_user_id,
        token_enc=cipher.encrypt(token.strip()),
        admin_ids=dump_admin_ids(admin_ids),
        is_default=False,
        enabled=True,
        note=(note or "").strip() or None,
    )
    session.add(bot)
    await session.flush()
    if is_default:
        await _clear_other_defaults(session, bot.id)
        bot.is_default = True
    await session.commit()
    await session.refresh(bot)
    return bot


async def update_bot(
    session: AsyncSession,
    config: AppConfig,
    bot_id: int,
    *,
    name: str | None = None,
    token: str | None = None,
    admin_ids: list[int] | None = None,
    is_default: bool | None = None,
    enabled: bool | None = None,
    note: str | None = None,
    client_factory: Any = None,
) -> ControlBot:
    """更新控制 Bot；换 Token 会重新校验并刷新资料。"""
    bot = await get_bot(session, bot_id)
    if bot is None:
        raise NotFoundError("机器人不存在")

    if name is not None:
        alias = name.strip()
        if not alias:
            raise ValidationFailedError("机器人名称不能为空")
        existing = await get_bot_by_name(session, alias)
        if existing is not None and existing.id != bot.id:
            raise UserExistsError("该名称已存在")
        bot.name = alias
    if note is not None:
        bot.note = note.strip() or None
    if admin_ids is not None:
        bot.admin_ids = dump_admin_ids(admin_ids)
    if enabled is not None:
        bot.enabled = enabled
    if token is not None:
        profile = await validate_bot_token(config, token, client_factory=client_factory)
        cipher = FieldCipher.from_config(config)
        bot.token_enc = cipher.encrypt(token.strip())
        bot.bot_username = profile.username
        bot.bot_telegram_id = profile.tg_user_id

    if is_default is True:
        await _clear_other_defaults(session, bot.id)
        bot.is_default = True
    elif is_default is False:
        bot.is_default = False

    await session.commit()
    await session.refresh(bot)
    return bot


async def delete_bot(session: AsyncSession, bot_id: int) -> ControlBot:
    """解绑控制 Bot。"""
    bot = await get_bot(session, bot_id)
    if bot is None:
        raise NotFoundError("机器人不存在")
    await session.delete(bot)
    await session.commit()
    return bot


async def _clear_other_defaults(session: AsyncSession, keep_id: int) -> None:
    others = await session.scalars(
        select(ControlBot).where(ControlBot.id != keep_id, ControlBot.is_default.is_(True))
    )
    for item in others:
        item.is_default = False
