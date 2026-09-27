"""代理工作台：额度总览、开号、下级树、划拨回收、续期与流水（P3-06）。

入口只有一个：**代理账号或平台账号**可访问（``require_agent_or_platform``）。
会员账号访问这里一律 403；代理访问任何业务接口也一律 403——两边彻底分开。

平台账号在这里的角色：
- 能开会员/试用/下级代理，开出来的号不占任何代理额度（``quota_type='none'``）；
- 不能在代理之间划拨（``allocate``/``reclaim`` 只对代理开放），需要发放额度时用
  ``POST /api/agent/{user_id}/adjust`` 调账。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    current_identity,
    require_agent_or_platform,
    require_platform,
    session_dependency,
)
from app.api.schemas.agent import (
    AdjustRequest,
    AgentOpenRequest,
    AllocateRequest,
    EnableRequest,
    MemberOpenRequest,
    ReclaimRequest,
    RenewRequest,
    TrialOpenRequest,
    UpgradeTrialRequest,
)
from app.core.config import AppConfig
from app.core.errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from app.db.models import User
from app.services import (
    agent_service,
    provision_service,
    quota_service,
    tenant_module_service,
    tenant_service,
    user_service,
)

router = APIRouter(
    prefix="/api/agent",
    tags=["agent"],
    dependencies=[Depends(require_agent_or_platform)],
)


async def current_actor(
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> User:
    """当前操作者必须是真实登录账号（API Token 不带 user_id，不能用代理接口）。"""
    value = identity.get("user_id")
    if value is None:
        raise PermissionDeniedError("代理工作台需要用登录账号操作")
    actor = await user_service.get_user(session, int(value))
    if actor is None or not actor.enabled:
        raise PermissionDeniedError("当前账号不可用")
    return actor


async def _subordinate_tenant(
    session: AsyncSession,
    *,
    actor: User,
    user_id: int,
) -> tuple[User, Any]:
    """取直属下级及其租户（续期要用）。"""
    target = await agent_service.get_direct_subordinate(session, actor=actor, user_id=user_id)
    tenant = None
    if target.tenant_id is not None:
        tenant = await tenant_service.get_tenant(session, target.tenant_id)
    return target, tenant


# --------------------------------------------------------------------------- #
# 总览
# --------------------------------------------------------------------------- #
@router.get("/quota")
async def get_quota(
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """额度总览：三个余额 + 已开出去还没到期的占用数。"""
    row = await quota_service.get_quota(session, actor.id)
    held = await agent_service.sum_held_quota(session, actor=actor)
    return {
        "user_id": actor.id,
        "username": actor.username,
        "account_type": actor.account_type,
        "quota": quota_service.serialize_quota(row),
        "held": held,
        "unlimited": row is None,
    }


@router.get("/stats")
async def get_stats(
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """业绩统计。"""
    return await agent_service.agent_stats(session, actor=actor)


@router.get("/expiring")
async def get_expiring(
    days: int = Query(default=agent_service.EXPIRING_SOON_DAYS, ge=1, le=90),
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """到期预警：名下即将到期 / 已过期的客户。"""
    items = await agent_service.expiring_tenants(session, actor=actor, days=days)
    return {"items": items, "total": len(items), "days": days}


@router.get("/templates")
async def list_templates(
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """可选功能包模板（开号表单用）。"""
    rows = await tenant_module_service.list_plan_templates(session)
    return {"items": [tenant_module_service.plan_template_payload(row) for row in rows]}


@router.get("/subordinates")
async def list_subordinates(
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """整棵子树（直属可操作，隔层只读）。"""
    items = await agent_service.list_subordinates(session, actor=actor)
    return {"items": items, "total": len(items)}


@router.get("/ledger")
async def list_ledger(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    quota_type: str | None = Query(default=None),
    action: str | None = Query(default=None),
    scope: str = Query(default="self", pattern="^(self|subtree)$"),
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """额度流水：默认只看自己的，``scope=subtree`` 看整棵子树。"""
    if scope == "subtree":
        ids = await agent_service.subtree_ids(session, actor.id)
        rows, total = await quota_service.list_ledger(
            session,
            subject_user_ids=ids,
            quota_type=quota_type,
            action=action,
            limit=limit,
            offset=offset,
        )
    else:
        rows, total = await quota_service.list_ledger(
            session,
            subject_user_id=actor.id,
            quota_type=quota_type,
            action=action,
            limit=limit,
            offset=offset,
        )
    return {
        "items": [quota_service.serialize_ledger(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# --------------------------------------------------------------------------- #
# 开号
# --------------------------------------------------------------------------- #
@router.post("/members", status_code=201)
async def open_member(
    payload: MemberOpenRequest,
    request: Request,
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """开正式会员。"""
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
    )


@router.post("/trials", status_code=201)
async def open_trial(
    payload: TrialOpenRequest,
    request: Request,
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """开 1 天试用账号（天数固定）。"""
    config: AppConfig = request.app.state.config
    return await provision_service.open_trial(
        session,
        config,
        actor=actor,
        username=payload.username,
        template_code=payload.template_code,
        password=payload.password,
        display_name=payload.display_name,
        note=payload.note,
    )


@router.post("/agents", status_code=201)
async def open_agent(
    payload: AgentOpenRequest,
    request: Request,
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """开下级代理（可同时划拨额度，落库是两笔）。"""
    config: AppConfig = request.app.state.config
    return await provision_service.open_agent(
        session,
        config,
        actor=actor,
        username=payload.username,
        password=payload.password,
        display_name=payload.display_name,
        allocate=payload.allocate,
        note=payload.note,
    )


# --------------------------------------------------------------------------- #
# 额度划拨与回收
# --------------------------------------------------------------------------- #
@router.post("/allocate")
async def allocate(
    payload: AllocateRequest,
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """把额度划拨给直属下级。"""
    target = await agent_service.get_direct_subordinate(
        session, actor=actor, user_id=payload.target_user_id
    )
    return await quota_service.allocate(
        session,
        actor=actor,
        target=target,
        quota_type=payload.quota_type,
        count=payload.count,
        note=payload.note,
    )


@router.post("/reclaim")
async def reclaim(
    payload: ReclaimRequest,
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """回收直属下级的未使用额度。"""
    target = await agent_service.get_direct_subordinate(
        session, actor=actor, user_id=payload.target_user_id
    )
    return await quota_service.reclaim(
        session,
        actor=actor,
        target=target,
        quota_type=payload.quota_type,
        count=payload.count,
        note=payload.note,
    )


@router.post("/{user_id}/adjust")
async def adjust(
    user_id: int,
    payload: AdjustRequest,
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
    _: None = Depends(require_platform),
) -> dict[str, Any]:
    """平台手工调账（可正可负，必须填备注）。"""
    target = await user_service.get_user(session, user_id)
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
# 下级账号管理
# --------------------------------------------------------------------------- #
@router.post("/{user_id}/renew")
async def renew_subordinate(
    user_id: int,
    payload: RenewRequest,
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """给直属下级的会员账号续期（不自动恢复线路运行）。"""
    target, tenant = await _subordinate_tenant(session, actor=actor, user_id=user_id)
    if tenant is None:
        raise ValidationFailedError("该账号还没有开通租户")
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


@router.post("/{user_id}/upgrade")
async def upgrade_subordinate_trial(
    user_id: int,
    payload: UpgradeTrialRequest,
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """把直属下级的 1 天试用号转成正式会员（不换账号）。"""
    target, tenant = await _subordinate_tenant(session, actor=actor, user_id=user_id)
    if tenant is None:
        raise ValidationFailedError("该账号还没有开通租户")
    result = await provision_service.upgrade_trial(
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


@router.post("/{user_id}/enable")
async def enable_subordinate(
    user_id: int,
    payload: EnableRequest,
    actor: User = Depends(current_actor),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """停用 / 解停直属下级（设置保留，功能全停）。"""
    return await agent_service.set_subordinate_enabled(
        session,
        actor=actor,
        user_id=user_id,
        enabled=payload.enabled,
        reason=payload.reason,
    )
