"""控制 Bot 管理：仅超级管理员可用。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role, session_dependency
from app.api.schemas.telegram import BotCreateRequest, BotUpdateRequest
from app.core.config import AppConfig
from app.db.base import as_utc
from app.db.models import ROLE_SUPER_ADMIN, ControlBot
from app.services import bot_service

router = APIRouter(
    prefix="/api/bots",
    tags=["bots"],
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)


def serialize_bot(bot: ControlBot) -> dict[str, Any]:
    """Bot 对外结构（不含 Token）。"""
    return {
        "id": bot.id,
        "name": bot.name,
        "bot_username": bot.bot_username,
        "bot_telegram_id": bot.bot_telegram_id,
        "admin_ids": bot_service.load_admin_ids(bot),
        "is_default": bot.is_default,
        "enabled": bot.enabled,
        "note": bot.note,
        "has_token": bool(bot.token_enc),
        "created_at": as_utc(bot.created_at),
        "updated_at": as_utc(bot.updated_at),
    }


def _client_factory(request: Request) -> Any:
    """测试可注入替身；生产为 None（走真实 Telethon）。"""
    return getattr(request.app.state, "bot_client_factory", None)


@router.get("")
async def list_bots(
    enabled: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """控制 Bot 列表。"""
    rows, total = await bot_service.list_bots(
        session,
        enabled=enabled,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [serialize_bot(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("", status_code=201)
async def create_bot(
    payload: BotCreateRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """绑定控制 Bot：保存前会调用 Telegram 校验 Token。"""
    config: AppConfig = request.app.state.config
    bot = await bot_service.create_bot(
        session,
        config,
        name=payload.name,
        token=payload.token,
        admin_ids=payload.admin_ids,
        is_default=payload.is_default,
        note=payload.note,
        client_factory=_client_factory(request),
    )
    return serialize_bot(bot)


@router.patch("/{bot_id}")
async def update_bot(
    bot_id: int,
    payload: BotUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """更新控制 Bot；换 Token 会重新校验。"""
    config: AppConfig = request.app.state.config
    bot = await bot_service.update_bot(
        session,
        config,
        bot_id,
        name=payload.name,
        token=payload.token,
        admin_ids=payload.admin_ids,
        is_default=payload.is_default,
        enabled=payload.enabled,
        note=payload.note,
        client_factory=_client_factory(request),
    )
    return serialize_bot(bot)


@router.delete("/{bot_id}")
async def delete_bot(
    bot_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """解绑控制 Bot。"""
    bot = await bot_service.delete_bot(session, bot_id)
    return {"id": bot_id, "name": bot.name, "deleted": True}
