"""账户管理：仅超级管理员可用。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity, require_role, session_dependency
from app.api.schemas.user import UserCreateRequest, UserUpdateRequest
from app.core.config import AppConfig
from app.core.errors import NotFoundError
from app.db.base import as_utc
from app.db.models import ROLE_SUPER_ADMIN, User
from app.services import session_service, user_service

router = APIRouter(
    prefix="/api/users",
    tags=["users"],
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)


def serialize_user(user: User) -> dict[str, Any]:
    """账号对外结构（不含任何密码信息）。"""
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "enabled": user.enabled,
        "must_change_password": user.must_change_password,
        "is_builtin": user.is_builtin,
        "last_login_at": as_utc(user.last_login_at),
        "created_at": as_utc(user.created_at),
        "updated_at": as_utc(user.updated_at),
    }


@router.get("")
async def list_accounts(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    role: str | None = Query(default=None),
    enabled: bool | None = Query(default=None),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """分页查询账号列表。"""
    rows, total = await user_service.list_users(
        session,
        limit=limit,
        offset=offset,
        role=role,
        enabled=enabled,
    )
    return {
        "items": [serialize_user(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("", status_code=201)
async def create_account(
    payload: UserCreateRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """新增子管理员。"""
    config: AppConfig = request.app.state.config
    user = await user_service.create_user(
        session,
        config,
        username=payload.username,
        password=payload.password,
        role=payload.role,
        display_name=payload.display_name,
    )
    return serialize_user(user)


@router.patch("/{user_id}")
async def update_account(
    user_id: int,
    payload: UserUpdateRequest,
    request: Request,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """修改角色、启停、重置密码或显示名。"""
    config: AppConfig = request.app.state.config
    actor_id = identity.get("user_id")
    user = await user_service.update_user(
        session,
        config,
        actor_id=int(actor_id) if actor_id is not None else None,
        user_id=user_id,
        role=payload.role,
        enabled=payload.enabled,
        password=payload.password,
        display_name=payload.display_name,
    )
    revoked_sessions = 0
    if payload.enabled is False:
        revoked_sessions = await session_service.revoke_all_web_sessions(session, user.username)
    return {**serialize_user(user), "revoked_sessions": revoked_sessions}


@router.delete("/{user_id}")
async def delete_account(
    user_id: int,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """删除子管理员。"""
    actor_id = identity.get("user_id")
    user = await user_service.delete_user(
        session,
        actor_id=int(actor_id) if actor_id is not None else None,
        user_id=user_id,
    )
    return {"id": user_id, "username": user.username, "deleted": True}


@router.post("/{user_id}/revoke-sessions")
async def revoke_user_sessions(
    user_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """踢下线指定账号的全部会话。"""
    user = await user_service.get_user(session, user_id)
    if user is None:
        raise NotFoundError("账号不存在")
    count = await session_service.revoke_all_web_sessions(session, user.username)
    return {"username": user.username, "revoked": count}
