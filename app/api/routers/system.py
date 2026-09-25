"""运行状态查询。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity, session_dependency
from app.core.config import AppConfig
from app.core.heartbeat import heartbeat_age_seconds, read_status
from app.core.runtime_control import read_control
from app.db.models import User
from app.db.session import get_engine
from app.services import delivery_service, lead_service, user_service

router = APIRouter(
    prefix="/api/system",
    tags=["system"],
    dependencies=[Depends(current_identity)],
)


@router.get("/status")
async def status(
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """运行状态：数据库连通性、账号统计与保留策略。

    运行时（采集/投递进程）的心跳在 T1-03 实现，此处先返回 unknown。
    """
    config: AppConfig = request.app.state.config
    total_users = int(await session.scalar(select(func.count()).select_from(User)) or 0)
    active_super_admins = await user_service.count_active_super_admins(session)
    jobs = await delivery_service.job_stats(session)
    queue_size = int(jobs.get("pending", 0)) + int(jobs.get("retrying", 0))
    leads = await lead_service.lead_stats(session)

    heartbeat = read_status(config.path(config.runtime.status_file))
    control = read_control(config.path(config.runtime.control_file))
    runtime_state = (heartbeat or {}).get("status", "stopped")

    database_status = "ok"
    try:
        get_engine()
    except RuntimeError:
        database_status = "unavailable"

    return {
        "runtime": {
            "status": runtime_state,
            "pid": (heartbeat or {}).get("pid"),
            "heartbeat_at": (heartbeat or {}).get("heartbeat_at"),
            "heartbeat_age_seconds": heartbeat_age_seconds(heartbeat),
            "started_at": (heartbeat or {}).get("started_at"),
            "paused": control["paused"],
            "stop_requested": control["stop_requested"],
        },
        "counts": {
            "users": total_users,
            "active_super_admins": active_super_admins,
            "sources": 0,
            "targets": 0,
            "routes": 0,
            "queue_size": queue_size,
            "jobs": jobs,
            "leads": leads,
        },
        "retention": {
            "messages_days": config.retention.messages_raw_days,
            "leads_days": config.retention.leads_days,
            "archive_days": config.retention.archive_days,
        },
        "access_mode": config.app.access_mode,
        "environment": config.app.environment,
        "database": {"status": database_status, "url_scheme": config.database.url.split("+")[0]},
        "server_time": datetime.now(UTC).isoformat(timespec="seconds"),
    }
