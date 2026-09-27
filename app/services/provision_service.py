"""开号链路（P3-04）：开正式会员 / 开试用会员 / 开下级代理 / 到期释放 / 续期。

三条铁律：

1. **一个事务写完**：开号要同时写 ``users`` / ``tenants`` / ``tenant_modules`` /
   ``tenant_limits`` / ``agent_quotas`` / ``quota_ledger``，任何一步失败整体回滚，
   所以本模块的每一步都传 ``commit=False``，最后由这里统一提交；
2. **额度先占后开**：代理开号时先从自己的余额扣 1，再落业务数据；
   扣不动（额度不足）直接 :class:`InsufficientQuotaError`，不会留下半个账号；
3. **平台开号不占额度**：平台账号没有余额行，开的号记 ``quota_type='none'``、
   ``quota_held=False``，与代理池彻底分开。

到期时间一律走 :mod:`app.core.expiry`——按自然日算，切点是当天 23:59:59。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.expiry import expiry_for_days
from app.db.base import as_utc
from app.db.models import (
    ACCOUNT_TYPE_AGENT,
    ACCOUNT_TYPE_MEMBER,
    ACTION_OPEN_AGENT,
    ACTION_OPEN_MEMBER,
    ACTION_OPEN_TRIAL,
    QUOTA_AGENT,
    QUOTA_MEMBER,
    QUOTA_NONE,
    QUOTA_TRIAL,
    ROLE_VIEWER,
    TENANT_KIND_MEMBER,
    TENANT_STATUS_ACTIVE,
    Tenant,
    User,
)
from app.services import (
    quota_service,
    tenant_module_service,
    tenant_service,
    user_service,
)

# 试用固定 1 天，且只允许这两个模板（八项决议第 7 条）
TRIAL_DAYS = 1
TRIAL_TEMPLATE_CODES = ("trial_carry", "trial_monitor")


def resolve_initial_password(password: str | None) -> tuple[str, bool]:
    """开号下发的密码，返回 ``(密码, 是否平台统一初始密码)``。

    不指定就用 :data:`app.services.user_service.INITIAL_PASSWORD`（``a123456``），
    它是**临时口令**：登录后必须先改密（``must_change_password=True``），
    所以不套用户改密时的强度校验；调用方显式传了密码才按强度校验。
    """
    value = (password or "").strip()
    if value:
        return value, False
    return user_service.INITIAL_PASSWORD, True


PLAN_REQUIRED_MESSAGE = (
    "请至少选择一个功能块（搬运帖子 / 监听会员 / 资源发现）或一个功能包模板。"
    "没有功能块的账号只有「账号与机器人」，客户进去看不到也用不了任何业务功能"
)


def _ensure_plan_selected(modules: list[str] | None, template_code: str | None) -> None:
    """开号 / 改功能包时必须给出至少一个功能块，挡住"空白账号"。"""
    if not modules and not template_code:
        raise ValidationFailedError(PLAN_REQUIRED_MESSAGE)


def _is_agent(actor: User) -> bool:
    """代理开号才占额度；平台开号跳过额度校验。"""
    return actor.account_type == ACCOUNT_TYPE_AGENT


async def _resolve_plan(
    session: AsyncSession,
    *,
    template_code: str | None,
    modules: list[str] | None,
) -> tuple[list[str], dict[str, Any]]:
    """把「模板 code」与「手勾功能块」合成一份最终授权清单。

    手勾优先于模板；只给模板时用模板的模块与限额；两个都给时模板只提供限额。
    """
    wanted: list[str] = []
    limits: dict[str, Any] = {}
    if modules:
        wanted = [tenant_module_service.validate_module(item) for item in modules]
    if template_code:
        template = await tenant_module_service.get_plan_template(session, template_code)
        if template is None or not template.enabled:
            raise NotFoundError("功能包模板不存在或已停用")
        payload = tenant_module_service.plan_template_payload(template)
        if not wanted:
            wanted = list(payload["modules"])
        limits = dict(payload["limits"])
    return sorted(set(wanted)), limits


async def _apply_plan(
    session: AsyncSession,
    *,
    tenant_id: int,
    modules: list[str],
    limits: dict[str, Any],
    granted_by: str | None,
) -> None:
    """把授权清单写进租户（不提交）。"""
    await tenant_module_service.apply_modules(
        session,
        tenant_id=tenant_id,
        modules=modules,
        granted_by=granted_by,
    )
    await tenant_module_service.apply_limits(
        session,
        tenant_id=tenant_id,
        max_routes=limits.get("max_routes"),
        max_tg_accounts=limits.get("max_tg_accounts"),
        max_sources=limits.get("max_sources"),
        allow_export=bool(limits.get("allow_export", True)),
    )


async def _create_member_account(
    session: AsyncSession,
    config: AppConfig,
    *,
    actor: User,
    username: str,
    days: int,
    password: str | None,
    display_name: str | None,
    quota_type: str,
    note: str | None,
    owner_agent: User | None = None,
) -> tuple[User, Tenant, str]:
    """建「登录账号 + 会员租户」这一对，不提交。

    顺序不能反：``tenants.owner_user_id`` 指向 ``users.id``，而 ``users.tenant_id``
    又指回租户。先建账号（``tenant_id`` 留空）→ 建租户并绑账号 → 回填
    ``users.tenant_id``，这样两个方向的外键都是先有的那个。
    """
    password_value, default_password = resolve_initial_password(password)
    # 归属代理：平台后台可以指定「这个会员挂在哪个代理名下」，额度就从那个代理账上扣
    owner_agent_id = (
        owner_agent.id if owner_agent is not None else (actor.id if _is_agent(actor) else None)
    )
    user = await user_service.build_user(
        session,
        config,
        username=username,
        password=password_value,
        account_type=ACCOUNT_TYPE_MEMBER,
        parent_user_id=owner_agent_id,
        tenant_id=None,
        display_name=display_name or username,
        must_change_password=True,
        enforce_password_policy=not default_password,
    )
    tenant = await tenant_service.build_tenant(
        session,
        name=await tenant_service.unique_tenant_name(session, username),
        kind=TENANT_KIND_MEMBER,
        owner_user_id=user.id,
        owner_agent_id=owner_agent_id,
        quota_type=quota_type,
        quota_held=owner_agent_id is not None,
        expires_at=expiry_for_days(days),
        created_by=actor.username,
        note=note,
    )
    user.tenant_id = tenant.id
    await session.flush()
    return user, tenant, password_value


def serialize_account(user: User) -> dict[str, Any]:
    """账号对外结构（不含任何密码信息）。"""
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "account_type": user.account_type,
        "role": user.role,
        "enabled": user.enabled,
        "must_change_password": user.must_change_password,
        "tenant_id": user.tenant_id,
        "parent_user_id": user.parent_user_id,
        "created_at": as_utc(user.created_at),
    }


def serialize_tenant(tenant: Tenant) -> dict[str, Any]:
    """租户对外结构。"""
    return {
        "id": tenant.id,
        "name": tenant.name,
        "kind": tenant.kind,
        "status": tenant.status,
        "expires_at": as_utc(tenant.expires_at),
        "owner_user_id": tenant.owner_user_id,
        "owner_agent_id": tenant.owner_agent_id,
        "quota_type": tenant.quota_type,
        "quota_held": tenant.quota_held,
        "note": tenant.note,
    }


def serialize_provision(
    *,
    user: User,
    tenant: Tenant,
    initial_password: str,
    modules: list[str],
    limits: dict[str, Any],
) -> dict[str, Any]:
    """开号结果的统一返回体；``initial_password`` 只在这一次明文返回。"""
    return {
        "account": serialize_account(user),
        "tenant": serialize_tenant(tenant),
        "initial_password": initial_password,
        "modules": modules,
        "module_labels": tenant_module_service.module_labels(modules),
        "limits": limits,
    }


async def open_member(
    session: AsyncSession,
    config: AppConfig,
    *,
    actor: User,
    username: str,
    days: int,
    password: str | None = None,
    display_name: str | None = None,
    modules: list[str] | None = None,
    template_code: str | None = None,
    note: str | None = None,
    owner_agent: User | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """开一个正式会员。

    额度从 **payer** 账上扣 1 个——指定归属代理时扣那个代理的，否则扣代理自己的；
    平台直开（没有归属代理）不占任何额度，记 ``quota_type='none'``。
    """
    _ensure_plan_selected(modules, template_code)
    wanted, limits = await _resolve_plan(session, template_code=template_code, modules=modules)
    payer = owner_agent if owner_agent is not None else (actor if _is_agent(actor) else None)
    user, tenant, password_value = await _create_member_account(
        session,
        config,
        actor=actor,
        username=username,
        days=days,
        password=password,
        display_name=display_name,
        quota_type=QUOTA_MEMBER if payer is not None else QUOTA_NONE,
        note=note,
        owner_agent=payer,
    )
    await _apply_plan(
        session,
        tenant_id=tenant.id,
        modules=wanted,
        limits=limits,
        granted_by=actor.username,
    )
    if payer is not None:
        await quota_service.consume(
            session,
            actor=payer,
            subject=payer,
            quota_type=QUOTA_MEMBER,
            action=ACTION_OPEN_MEMBER,
            related_tenant_id=tenant.id,
            related_user_id=user.id,
            note=note,
            commit=False,
        )
    if commit:
        await session.commit()
        await session.refresh(user)
        await session.refresh(tenant)
    return serialize_provision(
        user=user,
        tenant=tenant,
        initial_password=password_value,
        modules=wanted,
        limits=limits,
    )


async def open_trial(
    session: AsyncSession,
    config: AppConfig,
    *,
    actor: User,
    username: str,
    template_code: str | None = None,
    password: str | None = None,
    display_name: str | None = None,
    note: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """开一个 1 天试用账号（固定 1 天，只给搬运或监听的最小包）。"""
    code = (template_code or TRIAL_TEMPLATE_CODES[0]).strip()
    if code not in TRIAL_TEMPLATE_CODES:
        raise ValidationFailedError("试用只支持 " + "/".join(TRIAL_TEMPLATE_CODES) + " 两个模板")
    wanted, limits = await _resolve_plan(session, template_code=code, modules=None)
    # 试用包的限制写死，避免模板被改后试用跑偏
    trial_limits: dict[str, Any] = {"max_routes": 1, "allow_export": False}
    user, tenant, password_value = await _create_member_account(
        session,
        config,
        actor=actor,
        username=username,
        days=TRIAL_DAYS,
        password=password,
        display_name=display_name,
        quota_type=QUOTA_TRIAL,
        note=note or f"试用（{code}）",
    )
    await _apply_plan(
        session,
        tenant_id=tenant.id,
        modules=wanted,
        limits=trial_limits,
        granted_by=actor.username,
    )
    if _is_agent(actor):
        await quota_service.consume(
            session,
            actor=actor,
            subject=actor,
            quota_type=QUOTA_TRIAL,
            action=ACTION_OPEN_TRIAL,
            related_tenant_id=tenant.id,
            related_user_id=user.id,
            note=note,
            commit=False,
        )
    if commit:
        await session.commit()
        await session.refresh(user)
        await session.refresh(tenant)
    return serialize_provision(
        user=user,
        tenant=tenant,
        initial_password=password_value,
        modules=wanted,
        limits=trial_limits,
    )


async def upgrade_trial(
    session: AsyncSession,
    *,
    actor: User,
    tenant: Tenant,
    days: int,
    modules: list[str] | None = None,
    template_code: str | None = None,
    note: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """试用转正式：换账号不换人——释放试用额度、占用 1 个会员额度、解除试用限制。"""
    if tenant.kind != TENANT_KIND_MEMBER:
        raise ValidationFailedError("只有会员租户可以转正式")
    if tenant.quota_type != QUOTA_TRIAL:
        raise ConflictError("只有试用账号可以转为正式会员")

    agent = None
    if tenant.owner_agent_id is not None:
        agent = await user_service.get_user(session, tenant.owner_agent_id)

    if agent is not None and tenant.quota_held:
        await quota_service.release(
            session,
            subject=agent,
            quota_type=QUOTA_TRIAL,
            related_tenant_id=tenant.id,
            actor_username=actor.username,
            actor_user_id=actor.id,
            note=note or "试用转正式，释放试用额度",
            commit=False,
        )
    if agent is not None:
        await quota_service.renew_consume(
            session,
            actor=agent,
            subject=agent,
            related_tenant_id=tenant.id,
            note=note or "试用转正式，占用会员额度",
            commit=False,
        )

    if template_code is None and not modules:
        # 没指定就沿用试用期的功能块，只是把试用限制摘掉
        wanted = await tenant_module_service.list_modules(session, tenant.id)
        limits: dict[str, Any] = {}
    else:
        wanted, limits = await _resolve_plan(session, template_code=template_code, modules=modules)
    await _apply_plan(
        session,
        tenant_id=tenant.id,
        modules=wanted,
        limits=limits,
        granted_by=actor.username,
    )

    tenant.quota_type = QUOTA_MEMBER if agent is not None else QUOTA_NONE
    tenant.quota_held = agent is not None
    tenant.expires_at = expiry_for_days(days)
    tenant.status = TENANT_STATUS_ACTIVE
    await session.flush()
    if commit:
        await session.commit()
        await session.refresh(tenant)
    return {
        "tenant": serialize_tenant(tenant),
        "modules": wanted,
        "module_labels": tenant_module_service.module_labels(wanted),
        "limits": limits,
    }


async def open_agent(
    session: AsyncSession,
    config: AppConfig,
    *,
    actor: User,
    username: str,
    password: str | None = None,
    display_name: str | None = None,
    allocate: dict[str, int] | None = None,
    note: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """开一个下级代理（占 1 个代理额度，不占会员额度）。

    ``allocate`` 是"顺手划拨"的便捷参数，但落库是**两笔独立动作**：
    一笔 ``open_agent`` 扣代理额度，再来一次 ``allocate``（两行流水）。
    """
    password_value, default_password = resolve_initial_password(password)
    child = await user_service.build_user(
        session,
        config,
        username=username,
        password=password_value,
        role=ROLE_VIEWER,
        account_type=ACCOUNT_TYPE_AGENT,
        parent_user_id=actor.id,
        tenant_id=None,
        display_name=display_name or username,
        must_change_password=True,
        enforce_password_policy=not default_password,
    )
    await quota_service.get_or_create_quota(session, child.id)
    if _is_agent(actor):
        await quota_service.consume(
            session,
            actor=actor,
            subject=actor,
            quota_type=QUOTA_AGENT,
            action=ACTION_OPEN_AGENT,
            related_user_id=child.id,
            note=note,
            commit=False,
        )

    allocated: dict[str, int] = {}
    for quota_type, count in (allocate or {}).items():
        result = await quota_service.allocate(
            session,
            actor=actor,
            target=child,
            quota_type=quota_type,
            count=count,
            note=note or "开下级代理时划拨",
            commit=False,
        )
        allocated[result["quota_type"]] = int(result["count"])

    if commit:
        await session.commit()
        await session.refresh(child)
    return {
        "account": serialize_account(child),
        "initial_password": password_value,
        "allocated": allocated,
        "quota": quota_service.serialize_quota(await quota_service.get_quota(session, child.id)),
    }


async def expire_release(
    session: AsyncSession,
    *,
    tenant: Tenant,
    actor_username: str = "system",
    actor_user_id: int | None = None,
    note: str | None = None,
    commit: bool = True,
) -> bool:
    """到期释放额度：把当初占的 1 个额度还给开设它的代理。

    幂等：``quota_held=False`` 直接返回 ``False``，重复调用不会重复加额度。
    代理已被删除时只置 ``quota_held=False``，不写任何余额。
    """
    if not tenant.quota_held:
        return False
    tenant.quota_held = False
    if tenant.owner_agent_id is not None and tenant.quota_type in (
        QUOTA_MEMBER,
        QUOTA_TRIAL,
    ):
        agent = await user_service.get_user(session, tenant.owner_agent_id)
        if agent is not None:
            await quota_service.release(
                session,
                subject=agent,
                quota_type=tenant.quota_type,
                related_tenant_id=tenant.id,
                actor_username=actor_username,
                actor_user_id=actor_user_id,
                note=note or "到期释放额度",
                commit=False,
            )
    await session.flush()
    if commit:
        await session.commit()
        await session.refresh(tenant)
    return True


async def renew(
    session: AsyncSession,
    *,
    actor: User,
    tenant: Tenant,
    days: int,
    modules: list[str] | None = None,
    template_code: str | None = None,
    note: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """续期：只改到期日与功能包，**不自动恢复线路运行**。

    额度规则（设计 §5.7）：额度已释放（``quota_held=False``）才重新扣 1 个会员额度；
    还没到期、额度仍握着的时候提前续费**不重复占用**。

    额度永远从**开设它的代理**账上扣（``owner_agent_id``），与谁点的续期无关；
    平台开的号（``owner_agent_id`` 为空）不占任何额度。
    """
    if tenant.kind != TENANT_KIND_MEMBER:
        raise ValidationFailedError("只有会员租户可以续期")
    expires = expiry_for_days(days)

    agent = None
    if tenant.owner_agent_id is not None:
        agent = await user_service.get_user(session, tenant.owner_agent_id)

    if agent is not None:
        if not tenant.quota_held:
            await quota_service.renew_consume(
                session,
                actor=agent,
                subject=agent,
                related_tenant_id=tenant.id,
                note=note or "续期占用会员额度",
                commit=False,
            )
            tenant.quota_held = True
        tenant.quota_type = QUOTA_MEMBER
    else:
        tenant.quota_type = QUOTA_NONE
        tenant.quota_held = False

    if template_code is not None or modules:
        wanted, limits = await _resolve_plan(session, template_code=template_code, modules=modules)
        await _apply_plan(
            session,
            tenant_id=tenant.id,
            modules=wanted,
            limits=limits,
            granted_by=actor.username,
        )

    tenant.expires_at = expires
    tenant.status = TENANT_STATUS_ACTIVE
    await session.flush()
    if commit:
        await session.commit()
        await session.refresh(tenant)
    return {"tenant": serialize_tenant(tenant)}


async def set_plan(
    session: AsyncSession,
    *,
    actor: User,
    tenant: Tenant,
    modules: list[str] | None = None,
    template_code: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """改功能包（P5-03）：只改授权清单与用量限制，不动到期日与运行开关。

    前端菜单与接口守卫读的都是 ``tenant_modules``，所以改完立即生效；
    已经配好的 TG 账号、机器人、线路、线索一律保留。
    """
    if tenant.kind != TENANT_KIND_MEMBER:
        raise ValidationFailedError("只有会员租户可以改功能包")
    _ensure_plan_selected(modules, template_code)
    wanted, limits = await _resolve_plan(session, template_code=template_code, modules=modules)
    await _apply_plan(
        session,
        tenant_id=tenant.id,
        modules=wanted,
        limits=limits,
        granted_by=actor.username,
    )
    await session.flush()
    if commit:
        await session.commit()
        await session.refresh(tenant)
    return {
        "tenant": serialize_tenant(tenant),
        "modules": wanted,
        "module_labels": tenant_module_service.module_labels(wanted),
        "limits": limits,
    }


def expires_within(expires_at: datetime | None, days: int) -> bool:
    """到期预警用：是否会在 ``days`` 天内到期（已过期也算）。"""
    if expires_at is None:
        return False
    moment = datetime.now(UTC)
    target = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
    return target <= moment + timedelta(days=int(days))
