"""平台账号维护（P5）：编辑（改显示名 / 重置密码）与删除代理、会员账号。

入口只有平台后台（路由层 ``require_platform`` 守卫），代理与会员碰不到。
三条口径：

1. **编辑只做两件事**：改显示名、重置密码。重置下发统一初始密码 ``a123456``
   （或调用方指定的新密码），改完置 ``must_change_password=True`` 并撤销该账号
   的全部在线会话——对方要重新登录，登录后第一件事就是自己改密；
2. **删除是硬删**：会员账号连同它的租户一起删，租户下的 TG 账号、机器人、线路、
   线索等业务数据按外键 ``ON DELETE CASCADE`` 一并清除，不可恢复；
3. **先清干净再删**：名下还有下级、还有占用额度的会员、还有没回收的额度余额
   （会员 / 代理 / 试用）时一律拒绝，并说清差在哪——避免"删掉账号、额度凭空消失"。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.errors import (
    AgentHasSubordinatesError,
    BuiltinPasswordEnvError,
    ConflictError,
    SelfOperationError,
    ValidationFailedError,
)
from app.core.security import hash_password
from app.db.models import (
    ACCOUNT_TYPE_AGENT,
    ACCOUNT_TYPE_MEMBER,
    QUOTA_LABELS,
    Tenant,
    User,
)
from app.services import (
    provision_service,
    quota_service,
    session_service,
    tenant_service,
    user_service,
)


async def _children_counts(session: AsyncSession, user_id: int) -> dict[str, int]:
    """名下直属下级数量（代理 / 会员分开数）。"""
    rows = await session.execute(
        select(User.account_type, func.count())
        .where(User.parent_user_id == user_id)
        .group_by(User.account_type)
    )
    counts = {account_type: int(count) for account_type, count in rows.all()}
    return {
        "agents": counts.get(ACCOUNT_TYPE_AGENT, 0),
        "members": counts.get(ACCOUNT_TYPE_MEMBER, 0),
    }


async def _held_counts(session: AsyncSession, user_id: int) -> dict[str, int]:
    """这个代理名下仍在占用额度的租户（按额度类型计数，余额为 0 的类目不出现）。"""
    rows = await session.execute(
        select(Tenant.quota_type, func.count())
        .where(Tenant.owner_agent_id == user_id, Tenant.quota_held.is_(True))
        .group_by(Tenant.quota_type)
    )
    return {quota_type: int(count) for quota_type, count in rows.all() if int(count) > 0}


def _format_counts(counts: dict[str, int]) -> str:
    """把 ``{额度类型: 数量}`` 写成「会员额度 3、试用额度 1」这样的提示串。"""
    return "、".join(f"{QUOTA_LABELS.get(key, key)} {value}" for key, value in counts.items())


async def update_account(
    session: AsyncSession,
    config: AppConfig,
    *,
    actor: User,
    target: User,
    display_name: str | None = None,
    password: str | None = None,
    reset_password: bool = False,
    commit: bool = True,
) -> dict[str, Any]:
    """平台编辑账号：改显示名 / 重置密码（重置后对方必须重新登录并自己改密）。"""
    new_password: str | None = None
    if password is not None and password.strip():
        # 平台手填的新密码按正常强度校验走
        user_service.validate_password(password, min_length=config.security.password_min_length)
        new_password = password
    elif reset_password:
        new_password = user_service.INITIAL_PASSWORD

    if display_name is None and new_password is None:
        raise ValidationFailedError("没有需要修改的内容")

    if new_password is not None:
        if target.is_builtin:
            raise BuiltinPasswordEnvError()
        if target.id == actor.id:
            raise SelfOperationError("不能在这里重置自己的密码，请用右上角「修改密码」")

    revoked = 0
    if display_name is not None:
        target.display_name = (display_name or "").strip() or None
    if new_password is not None:
        target.password_hash = hash_password(new_password)
        # 重置出来的依旧是临时口令：首登必须自己改掉
        target.must_change_password = True
        revoked = await session_service.revoke_all_web_sessions(
            session,
            target.username,
            commit=False,
        )

    await session.flush()
    if commit:
        await session.commit()
        await session.refresh(target)
    return {
        "account": provision_service.serialize_account(target),
        "initial_password": new_password,
        "revoked_sessions": revoked,
    }


async def delete_account(
    session: AsyncSession,
    *,
    actor: User,
    target: User,
) -> dict[str, Any]:
    """平台删除代理 / 会员账号：硬删，先把"没清干净的"挡在前面。"""
    if target.is_builtin:
        raise ConflictError("内置管理员不可删除")
    if actor is not None and target.id == actor.id:
        raise SelfOperationError()
    if target.account_type not in (ACCOUNT_TYPE_AGENT, ACCOUNT_TYPE_MEMBER):
        raise ValidationFailedError("平台账号请到「账号管理」里维护")

    children = await _children_counts(session, target.id)
    if children["agents"] or children["members"]:
        raise AgentHasSubordinatesError(
            f"该账号名下还有 {children['agents']} 个下级代理、"
            f"{children['members']} 个会员账号，请先处理这些下级再删除"
        )

    held = await _held_counts(session, target.id)
    if held:
        detail = _format_counts(held)
        raise ConflictError(
            f"该代理名下还有占用额度、没到期的账号（{detail} 个），请先处理这些账号"
        )

    if target.account_type == ACCOUNT_TYPE_AGENT:
        balances = quota_service.balances(await quota_service.get_quota(session, target.id))
        left = {key: value for key, value in balances.items() if value > 0}
        if left:
            detail = _format_counts(left)
            raise ConflictError(f"该代理还有没回收的额度（{detail}），请先回收或用调账清零再删除")

    tenant_deleted = False
    quota_released = False
    if target.tenant_id is not None:
        tenant = await tenant_service.get_tenant(session, target.tenant_id)
        if tenant is not None:
            # 会员没了，当初占的那 1 格额度按"到期释放"还给开设它的代理，
            # 免得代理账上凭空少一格（幂等：本来就没占额度时什么都不做）
            quota_released = await provision_service.expire_release(
                session,
                tenant=tenant,
                actor_username=actor.username if actor else "system",
                actor_user_id=actor.id if actor else None,
                note="平台删除会员账号，释放额度",
                commit=False,
            )
            # 租户一删，它名下的 TG 账号 / 机器人 / 线路 / 线索按外键级联清除
            await session.delete(tenant)
            await session.flush()
            tenant_deleted = True

    user_id = target.id
    username = target.username
    account_type = target.account_type
    # 复用账号服务：它会再兜一次"内置 / 自己 / 最后一个超管"，并提交事务
    await user_service.delete_user(session, actor_id=actor.id if actor else None, user_id=user_id)
    return {
        "deleted": True,
        "user_id": user_id,
        "username": username,
        "account_type": account_type,
        "tenant_deleted": tenant_deleted,
        "quota_released": quota_released,
    }
