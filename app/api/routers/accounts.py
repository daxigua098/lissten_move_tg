"""执行账号池管理：仅超级管理员可用。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity, require_member_or_platform, session_dependency
from app.api.schemas.telegram import (
    AccountCreateRequest,
    AccountUpdateRequest,
    LoginCodeRequest,
    LoginPasswordRequest,
    LoginStartRequest,
)
from app.core.config import AppConfig, ConfigError, load_config
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.telegram_client import session_file_path
from app.db.base import as_utc
from app.db.models import ACCOUNT_PURPOSE_OUTREACH, ROLE_SUPER_ADMIN, TgAccount
from app.services import outreach_account_service, tg_account_service, tg_login_service

router = APIRouter(
    prefix="/api/accounts",
    tags=["accounts"],
    dependencies=[Depends(require_member_or_platform(ROLE_SUPER_ADMIN))],
)


def serialize_account(config: AppConfig, account: TgAccount) -> dict[str, Any]:
    """账号对外结构（绝不含明文凭据）。"""
    return {
        "id": account.id,
        "name": account.name,
        "purpose": account.purpose,
        "purpose_label": tg_account_service.describe_purpose(account.purpose),
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
        "owner_confirmed_at": as_utc(account.owner_confirmed_at),
        "owner_confirmed_by": account.owner_confirmed_by,
        "created_at": as_utc(account.created_at),
        "updated_at": as_utc(account.updated_at),
    }


async def serialize(session: AsyncSession, config: AppConfig, account: TgAccount) -> dict[str, Any]:
    """账号对外结构；发信息账号额外带上额度与冷却快照。"""
    data = serialize_account(config, account)
    if account.purpose == ACCOUNT_PURPOSE_OUTREACH:
        data["outreach"] = await outreach_account_service.snapshot(session, account)
    return data


def _account_client_factory(request: Request) -> Any:
    """测试与演练模式注入的客户端工厂；生产为 None。"""
    return getattr(request.app.state, "account_client_factory", None)


@router.get("")
async def list_accounts(
    request: Request,
    status: str | None = Query(default=None),
    purpose: str | None = Query(default=None),
    include_retired: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """账号列表；``purpose`` 区分执行账号与发信息账号。"""
    config: AppConfig = request.app.state.config
    rows, total = await tg_account_service.list_accounts(
        session,
        status=status,
        purpose=purpose,
        limit=limit,
        offset=offset,
        include_retired=include_retired,
    )
    return {
        "items": [await serialize(session, config, row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("", status_code=201)
async def create_account(
    payload: AccountCreateRequest,
    request: Request,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """登记账号（凭据加密保存，之后完成 Telegram 登录）。"""
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
        purpose=payload.purpose,
        owner_confirmed=payload.owner_confirmed,
        owner_confirmed_by=identity.get("username"),
    )
    return await serialize(session, config, account)


@router.patch("/{account_id}")
async def update_account(
    account_id: int,
    payload: AccountUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """更新账号信息、登录状态，或发信息账号的档位与运营态。"""
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
    if payload.outreach_tier is not None or payload.outreach_state is not None:
        if account.purpose != ACCOUNT_PURPOSE_OUTREACH:
            raise ValidationFailedError("只有发信息账号可以调整档位与运营态")
        await outreach_account_service.update_state(
            session,
            account,
            tier=payload.outreach_tier,
            state=payload.outreach_state,
        )
    return await serialize(session, config, account)


@router.delete("/{account_id}")
async def delete_account(
    account_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """删除执行账号。"""
    account = await tg_account_service.delete_account(session, account_id)
    return {"id": account_id, "name": account.name, "deleted": True}


@router.post("/{account_id}/credentials/refresh")
async def refresh_credentials(
    account_id: int,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """用 .env 里的默认 API 凭据覆盖该账号保存的凭据。

    这里重新从磁盘读取配置，这样改完 .env 点按钮即可生效，不必重启服务。
    """
    config: AppConfig = request.app.state.config
    try:
        fresh = load_config(
            config.config_path,
            env_file=config.env_path,
            project_root=config.project_root,
        )
    except ConfigError as exc:
        raise ValidationFailedError(f"读取配置失败：{exc}") from exc

    account = await tg_account_service.get_account(session, account_id)
    if account is None:
        raise NotFoundError("执行账号不存在")
    account = await tg_account_service.refresh_credentials_from_config(
        session,
        config,
        account,
        api_id=fresh.telegram.api_id,
        api_hash=fresh.telegram.api_hash,
    )
    return await serialize(session, config, account)


@router.post("/{account_id}/login/start")
async def start_login(
    account_id: int,
    payload: LoginStartRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """第一步：让 Telegram 发送登录验证码。"""
    config: AppConfig = request.app.state.config
    account = await tg_account_service.get_account(session, account_id)
    if account is None:
        raise NotFoundError("执行账号不存在")
    return await tg_login_service.start_login(
        config,
        account,
        client_factory=_account_client_factory(request),
        force_sms=payload.force_sms,
    )


@router.post("/{account_id}/login/verify")
async def verify_login_code(
    account_id: int,
    payload: LoginCodeRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """第二步：提交验证码（可能需要第三步的两步验证密码）。"""
    config: AppConfig = request.app.state.config
    account = await tg_account_service.get_account(session, account_id)
    if account is None:
        raise NotFoundError("执行账号不存在")
    return await tg_login_service.verify_code(
        session,
        config,
        account,
        code=payload.code,
        client_factory=_account_client_factory(request),
    )


@router.post("/{account_id}/login/password")
async def submit_login_password(
    account_id: int,
    payload: LoginPasswordRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """第三步：提交两步验证密码。"""
    config: AppConfig = request.app.state.config
    account = await tg_account_service.get_account(session, account_id)
    if account is None:
        raise NotFoundError("执行账号不存在")
    return await tg_login_service.submit_password(
        session,
        config,
        account,
        password=payload.password,
    )


@router.post("/{account_id}/login/cancel")
async def cancel_login(account_id: int) -> dict[str, Any]:
    """取消未完成的登录并断开连接。"""
    cancelled = await tg_login_service.cancel_login(account_id)
    return {"account_id": account_id, "cancelled": cancelled}


@router.get("/{account_id}/login/status")
async def login_status(account_id: int) -> dict[str, Any]:
    """查询该账号是否有未完成的登录。"""
    return tg_login_service.pending_status(account_id)
