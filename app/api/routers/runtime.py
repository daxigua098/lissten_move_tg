"""运行时控制：状态查询、暂停、恢复与停止。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role, session_dependency
from app.core.config import AppConfig
from app.core.heartbeat import heartbeat_age_seconds, read_status
from app.core.runtime_control import (
    read_control,
    set_paused,
    set_stop_requested,
)
from app.db.models import ROLE_SUB_ADMIN
from app.services import delivery_service

router = APIRouter(prefix="/api/runtime", tags=["runtime"])


async def _snapshot(config: AppConfig, session: AsyncSession) -> dict[str, Any]:
    """运行时快照：心跳 + 控制开关 + 队列统计。"""
    control_path = config.path(config.runtime.control_file)
    heartbeat = read_status(config.path(config.runtime.status_file))
    control = read_control(control_path)
    jobs = await delivery_service.job_stats(session)
    return {
        "status": (heartbeat or {}).get("status", "stopped"),
        "pid": (heartbeat or {}).get("pid"),
        "heartbeat_at": (heartbeat or {}).get("heartbeat_at"),
        "heartbeat_age_seconds": heartbeat_age_seconds(heartbeat),
        "started_at": (heartbeat or {}).get("started_at"),
        "paused": control["paused"],
        "stop_requested": control["stop_requested"],
        "jobs": jobs,
        "queue_size": int(jobs.get("pending", 0)) + int(jobs.get("retrying", 0)),
    }


@router.get("/status", dependencies=[Depends(require_role(ROLE_SUB_ADMIN))])
async def runtime_status(
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """查看运行时状态。"""
    config: AppConfig = request.app.state.config
    return await _snapshot(config, session)


@router.post("/pause", dependencies=[Depends(require_role(ROLE_SUB_ADMIN))])
async def pause(
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """暂停投递（监听继续，任务继续入队）。"""
    config: AppConfig = request.app.state.config
    set_paused(config.path(config.runtime.control_file), True)
    return await _snapshot(config, session)


@router.post("/resume", dependencies=[Depends(require_role(ROLE_SUB_ADMIN))])
async def resume(
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """恢复投递。"""
    config: AppConfig = request.app.state.config
    control_path = config.path(config.runtime.control_file)
    set_stop_requested(control_path, False)
    set_paused(control_path, False)
    return await _snapshot(config, session)


@router.post("/stop", dependencies=[Depends(require_role(ROLE_SUB_ADMIN))])
async def stop(
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """请求运行时进程退出（当前任务结束后退出）。"""
    config: AppConfig = request.app.state.config
    control_path = config.path(config.runtime.control_file)
    set_stop_requested(control_path, True)
    set_paused(control_path, True)
    return await _snapshot(config, session)
