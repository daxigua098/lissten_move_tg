"""登录历史与审计日志查询。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity, require_role, session_dependency
from app.db.base import as_utc
from app.db.models import ROLE_SUPER_ADMIN
from app.services import audit_service, login_history_service

router = APIRouter(prefix="/api", tags=["logs"])


@router.get(
    "/login-history",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
async def login_history(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    username: str | None = Query(default=None),
    success: bool | None = Query(default=None),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """登录历史（仅超级管理员）。"""
    rows, total = await login_history_service.list_login_history(
        session,
        limit=limit,
        offset=offset,
        username=username,
        success=success,
    )
    return {
        "items": [
            {
                "id": row.id,
                "username": row.username,
                "ip_address": row.ip_address,
                "user_agent": row.user_agent,
                "success": row.success,
                "reason": row.reason,
                "created_at": as_utc(row.created_at),
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/audit", dependencies=[Depends(current_identity)])
async def audit_logs(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    username: str | None = Query(default=None),
    path: str | None = Query(default=None),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """审计日志（所有已认证角色可读）。"""
    rows, total = await audit_service.list_audit_logs(
        session,
        limit=limit,
        offset=offset,
        username=username,
        path=path,
    )
    return {
        "items": [
            {
                "id": row.id,
                "username": row.username,
                "method": row.method,
                "path": row.path,
                "status_code": row.status_code,
                "ip_address": row.ip_address,
                "created_at": as_utc(row.created_at),
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
