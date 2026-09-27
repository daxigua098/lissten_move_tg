"""平台后台（P5）：平台总览、代理管理、会员管理、额度台账、到期看板。

入口只有平台账号（``require_platform``）：代理与会员访问这里一律 403。

**写操作全部复用既有链路**——开号走 ``provision_service``、额度走 ``quota_service``、
启停走 ``agent_service``，这里只补一层"平台可以跨层级操作任意账号"的入口，
不新增第二套账。所有写请求都会落进审计日志（审计中间件统一记录）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity, require_platform, session_dependency
from app.api.schemas.platform import (
    PlatformAdjustRequest,
    PlatformEnableRequest,
    PlatformMemberOpenRequest,
    PlatformPlanRequest,
    PlatformRenewRequest,
)
from app.core.config import AppConfig
from app.core.errors import NotFoundError, ValidationFailedError
from app.db.models import ACCOUNT_TYPE_AGENT, User
from app.services import (
    agent_service,
    platform_service,
    provision_service,
    quota_service,
    tenant_module_service,
    tenant_service,
    user_service,
)

router = APIRouter(
    prefix="/api/platform",
    tags=["platform"],
    dependencies=[Depends(require_platform)],
)

# 台账导出的上限：一次最多导这么多行，避免把内存拉爆
EXPORT_LIMIT = 5000


async def current_actor(
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> User:
    """当前操作者必须是真实登录账号（API Token 不带 user_id，不能用平台后台）。"""
    value = identity.get("user_id")
    if value is None:
        raise ValidationFailedError("平台后台需要用登录账号操作")
    actor = await user_service.get_user(session, int(value))
    if actor is None or not actor.enabled:
        raise ValidationFailedError("当前账号不可用")
    return actor


async def _member_target(session: AsyncSession, user_id: int) -> tuple[User, Any]:
    """取会员账号及其租户；不是会员账号或没有租户 → 400。"""
    user = await user_service.get_user(session, int(user_id))
    if user is None:
        raise NotFoundError("账号不存在")
    if user.tenant_id is None:
        raise ValidationFailedError("该账号还没有开通租户")
    tenant = await tenant_service.get_tenant(session, user.tenant_id)
    if tenant is None:
        raise NotFoundError("租户不存在")
    return user, tenant


# --------------------------------------------------------------------------- #
# 总览与列表
# --------------------------------------------------------------------------- #
@router.get("/overview")
async def get_overview(
    days: int = Query(default=platform_service.EXPIRING_DAYS, ge=1, le=90),
    limit: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """平台总览：账号分布、今日动作、即将到期、额度预警（P5-01）。"""
    return await platform_service.overview(
        session,
        expiring_days=days,
        limit=limit,
    )


@router.get("/agents")
async def list_agents(
    keyword: str | None = Query(default=None, max_length=64),
    enabled: bool | None = Query(default=None),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """代理列表：上下级数 + 三类额度余额与占用（P5-02）。"""
    return await platform_service.list_agents(session, keyword=keyword, enabled=enabled)


@router.get("/agents/{user_id}/tree")
async def get_agent_tree(
    user_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """某个代理的整棵子树（只读）。"""
    return await platform_service.agent_tree(session, user_id=user_id)


@router.get("/members")
async def list_members(
    agent_user_id: int | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(active|expired|suspended)$"),
    module: str | None = Query(default=None, max_length=16),
    quota_type: str | None = Query(default=None, pattern="^(member|trial|none)$"),
    keyword: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """会员列表：按代理 / 状态 / 功能块 / 额度类型筛选（P5-03）。"""
    return await platform_service.list_members(
        session,
        agent_user_id=agent_user_id,
        status=status,
        module=module,
        quota_type=quota_type,
        keyword=keyword,
        limit=limit,
        offset=offset,
    )


@router.get("/expiry")
async def get_expiry_board(
    limit: int = Query(default=200, ge=1, le=500),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """到期看板：今日 / 3 天内 / 7 天内 / 已过期（最近 30 天）（P5-05）。"""
    return await platform_service.expiry_board(session, limit=limit)


@router.get("/templates")
async def list_templates(
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """可选功能包模板（开号表单用）。"""
    rows = await tenant_module_service.list_plan_templates(session)
    return {"items": [tenant_module_service.plan_template_payload(row) for row in rows]}


# --------------------------------------------------------------------------- #
# 会员管理
# --------------------------------------------------------------------------- #
@router.post("/members", status_code=201)
async def open_member(
    payload: PlatformMemberOpenRequest,
    request: Request,
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """平台开会员；填了归属代理就从那个代理账上扣 1 个会员额度。"""
    owner = None
    if payload.owner_agent_id is not None:
        owner = await user_service.get_user(session, int(payload.owner_agent_id))
        if owner is None:
            raise NotFoundError("归属代理不存在")
        if owner.account_type != ACCOUNT_TYPE_AGENT:
            raise ValidationFailedError("归属账号不是代理账号")
    config: AppConfig = request.app.state.config
    return await provision_service.open_member(
        session,
        config,
        actor=actor,
        username=payload.username,
        days=payload.days,
        password=payload.password,
        display_name=payload.display_name,
        modules=payload.modules,
        template_code=payload.template_code,
        note=payload.note,
        owner_agent=owner,
    )


@router.post("/members/{user_id}/renew")
async def renew_member(
    user_id: int,
    payload: PlatformRenewRequest,
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """平台续期：只改到期日与功能包，**不自动恢复线路运行**。"""
    target, tenant = await _member_target(session, user_id)
    result = await provision_service.renew(
        session,
        actor=actor,
        tenant=tenant,
        days=payload.days,
        modules=payload.modules,
        template_code=payload.template_code,
        note=payload.note,
    )
    result["account"] = provision_service.serialize_account(target)
    return result


@router.post("/members/{user_id}/plan")
async def set_member_plan(
    user_id: int,
    payload: PlatformPlanRequest,
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """改功能包：立即生效，已配的 TG 账号 / 机器人 / 线路全部保留。"""
    if payload.modules is None and payload.template_code is None:
        raise ValidationFailedError("请至少选择一个功能块或一个功能包模板")
    _target, tenant = await _member_target(session, user_id)
    return await provision_service.set_plan(
        session,
        actor=actor,
        tenant=tenant,
        modules=payload.modules,
        template_code=payload.template_code,
    )


@router.post("/accounts/{user_id}/enable")
async def enable_account(
    user_id: int,
    payload: PlatformEnableRequest,
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """停用 / 解停任意账号（不限层级）：功能全停、配置保留、额度不释放。"""
    target = await user_service.get_user(session, int(user_id))
    if target is None:
        raise NotFoundError("账号不存在")
    return await agent_service.set_account_enabled(
        session,
        actor=actor,
        target=target,
        enabled=payload.enabled,
        reason=payload.reason,
    )


@router.post("/accounts/{user_id}/adjust")
async def adjust_account(
    user_id: int,
    payload: PlatformAdjustRequest,
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """平台手工调账（可正可负，必须填备注）。"""
    target = await user_service.get_user(session, int(user_id))
    if target is None:
        raise NotFoundError("账号不存在")
    balance = await quota_service.manual_adjust(
        session,
        actor=actor,
        target=target,
        quota_type=payload.quota_type,
        delta=payload.delta,
        note=payload.note,
    )
    row = await quota_service.get_quota(session, target.id)
    return {
        "user_id": target.id,
        "username": target.username,
        "quota_type": payload.quota_type,
        "delta": payload.delta,
        "balance": balance,
        "quota": quota_service.serialize_quota(row),
    }


# --------------------------------------------------------------------------- #
# 额度台账
# --------------------------------------------------------------------------- #
def _ledger_filters(
    agent_user_id: int | None,
    quota_type: str | None,
    action: str | None,
    start: datetime | None,
    end: datetime | None,
) -> dict[str, Any]:
    return {
        "agent_user_id": agent_user_id,
        "quota_type": quota_type,
        "action": action,
        "start": start,
        "end": end,
    }


@router.get("/ledger")
async def list_ledger(
    agent_user_id: int | None = Query(default=None),
    quota_type: str | None = Query(default=None, pattern="^(member|agent|trial)$"),
    action: str | None = Query(default=None, max_length=32),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """额度台账：全部划拨与消耗记录（P5-04）。"""
    return await platform_service.list_ledger(
        session,
        **_ledger_filters(agent_user_id, quota_type, action, start, end),
        limit=limit,
        offset=offset,
    )


@router.get("/ledger.csv")
async def export_ledger(
    agent_user_id: int | None = Query(default=None),
    quota_type: str | None = Query(default=None, pattern="^(member|agent|trial)$"),
    action: str | None = Query(default=None, max_length=32),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    session: AsyncSession = Depends(session_dependency),
) -> Response:
    """台账导出（CSV，带 BOM，Excel 直接打开）。"""
    result = await platform_service.list_ledger(
        session,
        **_ledger_filters(agent_user_id, quota_type, action, start, end),
        limit=EXPORT_LIMIT,
        offset=0,
    )
    body = platform_service.ledger_csv(result["items"])
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="quota-ledger.csv"'},
    )
