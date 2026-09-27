"""运行时控制：状态查询、暂停恢复、租户级启停（P4-06）。

两种口径，别混：

- **平台账号**：``/start`` ``/stop`` 仍然是"全局运行时进程"的开关，给运维用；
- **会员账号**：``/start`` ``/stop`` 操作的是**自己租户的运行总开关**
  （``tenants.runtime_enabled``），不会牵连别的租户。租户开关挂在投递路径上实时
  判断，所以它是"热"的；只有新增 / 删除线路、换监听源才需要「重启」进程。

``/pause`` ``/resume`` 是全局开关（一按全部租户都停），只留给平台账号。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    current_identity,
    require_member_or_platform,
    require_platform,
    require_role,
    session_dependency,
)
from app.core.config import AppConfig
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.heartbeat import heartbeat_age_seconds, is_running, read_status
from app.core.runtime_control import read_control, set_paused, set_stop_requested
from app.db.models import ACCOUNT_TYPE_MEMBER, ROLE_SUB_ADMIN, SELF_TENANT_ID, Tenant
from app.services import (
    delivery_service,
    runtime_service,
    tenant_runtime_service,
    tenant_service,
    tenant_status_service,
)

router = APIRouter(prefix="/api/runtime", tags=["runtime"])


def _is_member(identity: dict[str, Any]) -> bool:
    return identity.get("account_type") == ACCOUNT_TYPE_MEMBER


def _scope_tenant_id(identity: dict[str, Any]) -> int | None:
    """会员看自己租户的数据；平台看全局（返回 None）。"""
    if _is_member(identity):
        value = identity.get("tenant_id")
        return int(value) if value is not None else None
    return None


async def _member_tenant(session: AsyncSession, identity: dict[str, Any]) -> Tenant:
    """取当前会员的租户；没有租户就直接报错。"""
    tenant_id = identity.get("tenant_id")
    if tenant_id is None:
        raise ValidationFailedError("当前账号还没有开通租户，请联系你的上级")
    tenant = await tenant_service.get_tenant(session, int(tenant_id))
    if tenant is None:
        raise NotFoundError("租户不存在")
    return tenant


async def _tenant_block(session: AsyncSession, identity: dict[str, Any]) -> dict[str, Any] | None:
    """会员的租户状态块（运行总览顶部提示用）。"""
    tenant_id = _scope_tenant_id(identity)
    if tenant_id is None:
        return None
    tenant = await tenant_service.get_tenant(session, tenant_id)
    if tenant is None:
        return None
    state = tenant_status_service.evaluate(tenant)
    return {
        **tenant_status_service.state_payload(state),
        "stop_reason_label": tenant_status_service.stop_reason_label(state.runtime_stop_reason),
    }


async def _snapshot(
    config: AppConfig,
    session: AsyncSession,
    identity: dict[str, Any],
) -> dict[str, Any]:
    """运行时快照：心跳 + 控制开关 + 队列统计 + 租户状态。"""
    control_path = config.path(config.runtime.control_file)
    heartbeat = read_status(config.path(config.runtime.status_file))
    control = read_control(control_path)
    jobs = await delivery_service.job_stats(session)
    pending_routes = await runtime_service.pending_route_ids(
        session,
        config,
        tenant_id=_scope_tenant_id(identity),
    )
    # 进程被杀之后状态文件会停在 running：心跳过期就按「已停止」报，
    # 否则界面会一直显示「运行中」，而加群 / 探测 / 投递其实都没在跑
    status = (heartbeat or {}).get("status", "stopped")
    if status == "running" and not is_running(heartbeat):
        status = "stopped"
    return {
        "status": status,
        "pid": (heartbeat or {}).get("pid"),
        "heartbeat_at": (heartbeat or {}).get("heartbeat_at"),
        "heartbeat_age_seconds": heartbeat_age_seconds(heartbeat),
        "started_at": (heartbeat or {}).get("started_at"),
        "paused": control["paused"],
        "stop_requested": control["stop_requested"],
        "jobs": jobs,
        "queue_size": int(jobs.get("pending", 0)) + int(jobs.get("retrying", 0)),
        "pending_route_ids": pending_routes or [],
        "config_stale": bool(pending_routes),
        "tenant": await _tenant_block(session, identity),
    }


@router.get("/status", dependencies=[Depends(require_member_or_platform(ROLE_SUB_ADMIN))])
async def runtime_status(
    request: Request,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """查看运行时状态。"""
    config: AppConfig = request.app.state.config
    return await _snapshot(config, session, identity)


@router.post(
    "/pause",
    dependencies=[Depends(require_platform), Depends(require_role(ROLE_SUB_ADMIN))],
)
async def pause(
    request: Request,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """全局暂停投递（监听继续，任务继续入队）。"""
    config: AppConfig = request.app.state.config
    set_paused(config.path(config.runtime.control_file), True)
    return await _snapshot(config, session, identity)


@router.post("/start", dependencies=[Depends(require_member_or_platform(ROLE_SUB_ADMIN))])
async def start(
    request: Request,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """启动。

    - 会员：打开自己租户的运行开关（功能立即恢复；进程没在跑会顺手拉起来）；
    - 平台：拉起搬运运行时进程（后台独立进程）。
    """
    config: AppConfig = request.app.state.config
    result: dict[str, Any] = {}
    if _is_member(identity):
        tenant = await _member_tenant(session, identity)
        result["tenant"] = await tenant_runtime_service.start_tenant_runtime(
            session,
            tenant,
            actor_username=str(identity.get("username") or ""),
        )
    result["start"] = await runtime_service.start_runtime_process(config)
    return {**await _snapshot(config, session, identity), **result}


@router.post("/start-all", dependencies=[Depends(require_member_or_platform(ROLE_SUB_ADMIN))])
async def start_all(
    request: Request,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """一键启动：打开租户运行开关 + 把所有线路恢复为启用（P4-06 待确认项 5）。

    续期 / 解停之后，会员只需要点这一个按钮，不用逐条线路去开。
    """
    config: AppConfig = request.app.state.config
    tenant_id = _scope_tenant_id(identity) or SELF_TENANT_ID
    tenant = await tenant_service.get_tenant(session, tenant_id)
    if tenant is None:
        raise NotFoundError("租户不存在")
    enabled_routes = await tenant_runtime_service.enable_all_routes(session, tenant_id)
    tenant_state = await tenant_runtime_service.start_tenant_runtime(
        session,
        tenant,
        actor_username=str(identity.get("username") or ""),
    )
    process = await runtime_service.start_runtime_process(config)
    pending = await runtime_service.pending_route_ids(session, config, tenant_id=tenant_id)
    return {
        **await _snapshot(config, session, identity),
        "enabled_routes": enabled_routes,
        "tenant": tenant_state,
        "start": process,
        "pending_route_ids": pending or [],
        "need_restart": bool(pending),
    }


@router.post("/restart", dependencies=[Depends(require_member_or_platform(ROLE_SUB_ADMIN))])
async def restart(
    request: Request,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """重启搬运运行时：改完线路或接收目标后，用它让配置生效。"""
    config: AppConfig = request.app.state.config
    result = await runtime_service.restart_runtime_process(config)
    return {**await _snapshot(config, session, identity), "start": result}


@router.post(
    "/resume",
    dependencies=[Depends(require_platform), Depends(require_role(ROLE_SUB_ADMIN))],
)
async def resume(
    request: Request,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """恢复投递（全局）。"""
    config: AppConfig = request.app.state.config
    control_path = config.path(config.runtime.control_file)
    set_stop_requested(control_path, False)
    set_paused(control_path, False)
    return await _snapshot(config, session, identity)


@router.post("/stop", dependencies=[Depends(require_member_or_platform(ROLE_SUB_ADMIN))])
async def stop(
    request: Request,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """停止。

    - 会员：关掉自己租户的运行开关（别的租户不受影响），排队任务一并取消；
    - 平台：请求运行时进程退出（当前任务结束后退出）。
    """
    config: AppConfig = request.app.state.config
    result: dict[str, Any] = {}
    if _is_member(identity):
        tenant = await _member_tenant(session, identity)
        result["tenant"] = await tenant_runtime_service.stop_tenant_runtime(session, tenant)
    else:
        control_path = config.path(config.runtime.control_file)
        set_stop_requested(control_path, True)
        set_paused(control_path, True)
    return {**await _snapshot(config, session, identity), **result}
