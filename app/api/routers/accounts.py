"""执行账号池管理：仅超级管理员可用。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role, session_dependency
from app.api.schemas.telegram import AccountCreateRequest, AccountUpdateRequest
from app.core.config import AppConfig
from app.core.telegram_client import session_file_path
from app.db.base import as_utc
from app.db.models import ROLE_SUPER_ADMIN, TgAccount
from app.services import tg_account_service

router = APIRouter(
    prefix="/api/accounts",
    tags=["accounts"],
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)


def serialize_account(config: AppConfig, account: TgAccount) -> dict[str, Any]:
    """账号对外结构（绝不含明文凭据）。"""
    return {
        "id": account.id,
        "name": account.name,
        "phone_masked": account.phone_masked,
        "session_name": account.session_name,
        "session_file": f"{session_file_path(config, account.session_name)}.session",
        "is_default": account.is_default,
        "status": account.status,
        "status_label": tg_account_service.describe_status(account.status),
        "health_score": account.health_score,
        "consecutive_failures": account.consecutive_failures,
        "tg_user_id": account.tg_user_id,
        "username": account.username,
        "last_used_at": as_utc(account.last_used_at),
        "last_error": account.last_error,
        "note": account.note,
        "created_at": as_utc(account.created_at),
        "updated_at": as_utc(account.updated_at),
    }


@router.get("")
async def list_accounts(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """执行账号列表。"""
    config: AppConfig = request.app.state.config
    rows, total = await tg_account_service.list_accounts(
        session,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [serialize_account(config, row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("", status_code=201)
async def create_account(
    payload: AccountCreateRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """登记执行账号（凭据加密保存，之后用 CLI 完成登录）。"""
    config: AppConfig = request.app.state.config
    account = await tg_account_service.create_account(
        session,
        config,
        name=payload.name,
        phone=payload.phone,
        api_id=payload.api_id,
        api_hash=payload.api_hash,
        session_name=payload.session_name,
        is_default=payload.is_default,
        note=payload.note,
    )
    return serialize_account(config, account)


@router.patch("/{account_id}")
async def update_account(
    account_id: int,
    payload: AccountUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """更新账号信息或状态。"""
    config: AppConfig = request.app.state.config
    account = await tg_account_service.update_account(
        session,
        config,
        account_id,
        name=payload.name,
        note=payload.note,
        status=payload.status,
        is_default=payload.is_default,
        phone=payload.phone,
        api_id=payload.api_id,
        api_hash=payload.api_hash,
    )
    return serialize_account(config, account)


@router.delete("/{account_id}")
async def delete_account(
    account_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """删除执行账号。"""
    account = await tg_account_service.delete_account(session, account_id)
    return {"id": account_id, "name": account.name, "deleted": True}
