"""运行状态查询。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity, require_member_or_platform, session_dependency
from app.core.config import AppConfig
from app.core.heartbeat import heartbeat_age_seconds, read_status
from app.core.runtime_control import read_control
from app.db.models import ACCOUNT_TYPE_MEMBER, ACCOUNT_TYPE_PLATFORM, User
from app.db.session import get_engine
from app.services import (
    delivery_service,
    lead_service,
    runtime_service,
    tenant_service,
    tenant_status_service,
    user_service,
)

router = APIRouter(
    prefix="/api/system",
    tags=["system"],
    dependencies=[Depends(require_member_or_platform(None))],
)


@router.get("/status")
async def status(
    request: Request,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """运行状态：数据库连通性、账号统计与保留策略。

    运行时（采集/投递进程）的心跳在 T1-03 实现，此处先返回 unknown。

    平台级账号统计（账号总数、启用超管数）只对**平台账号**返回：
    会员看到的是自己账号的统计，不该知道平台上有几个后台账号。
    """
    config: AppConfig = request.app.state.config
    is_platform = identity.get("account_type") == ACCOUNT_TYPE_PLATFORM
    total_users = int(await session.scalar(select(func.count()).select_from(User)) or 0)
    active_super_admins = await user_service.count_active_super_admins(session)
    jobs = await delivery_service.job_stats(session)
    queue_size = int(jobs.get("pending", 0)) + int(jobs.get("retrying", 0))
    leads = await lead_service.lead_stats(session)

    heartbeat = read_status(config.path(config.runtime.status_file))
    control = read_control(config.path(config.runtime.control_file))
    runtime_state = (heartbeat or {}).get("status", "stopped")

    # 租户块（P4）：会员只看得到自己的；线路号也只算自己租户的，不泄露别人
    tenant_id = (
        identity.get("tenant_id") if identity.get("account_type") == ACCOUNT_TYPE_MEMBER else None
    )
    tenant_block = None
    if tenant_id is not None:
        tenant = await tenant_service.get_tenant(session, int(tenant_id))
        if tenant is not None:
            state = tenant_status_service.evaluate(tenant)
            tenant_block = {
                **tenant_status_service.state_payload(state),
                "stop_reason_label": tenant_status_service.stop_reason_label(
                    state.runtime_stop_reason
                ),
            }
    pending_routes = await runtime_service.pending_route_ids(
        session,
        config,
        tenant_id=int(tenant_id) if tenant_id is not None else None,
    )

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
            "pending_route_ids": pending_routes or [],
            "config_stale": bool(pending_routes),
        },
        "tenant": tenant_block,
        "counts": {
            "users": total_users if is_platform else None,
            "active_super_admins": active_super_admins if is_platform else None,
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
