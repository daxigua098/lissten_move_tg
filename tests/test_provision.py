"""P3-04：开号链路、到期释放与续期。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import (
    ConflictError,
    InsufficientQuotaError,
    NotFoundError,
    ValidationFailedError,
)
from app.core.expiry import expiry_for_days, resolve_timezone
from app.services.user_service import INITIAL_PASSWORD


async def _make_user(session, config, username, *, account_type="platform", parent_user_id=None):
    from app.services import user_service

    return await user_service.create_user(
        session,
        config,
        username=username,
        password="Passw0rd12345",
        account_type=account_type,
        parent_user_id=parent_user_id,
        must_change_password=False,
    )


async def _agent_with_quota(
    config,
    username,
    *,
    member=0,
    agent_quota=0,
    trial=0,
    parent_user_id=None,
):
    """建一个带指定余额的代理，返回 (agent_id, admin_id)。"""
    from app.db.models import AgentQuota
    from app.db.session import session_scope

    async with session_scope() as session:
        admin = await _make_user(session, config, f"{username}-admin")
        agent = await _make_user(
            session,
            config,
            username,
            account_type="agent",
            parent_user_id=parent_user_id,
        )
        session.add(
            AgentQuota(
                user_id=agent.id,
                member_quota=member,
                agent_quota=agent_quota,
                trial_quota=trial,
            )
        )
        await session.flush()
        return agent.id, admin.id


async def _load(session, user_id: int):
    from app.db.models import User

    return await session.get(User, user_id)


async def test_open_member_by_agent_consumes_one_member_quota(db) -> None:
    """代理开会员：扣 1 个会员额度，租户与账号一次写完。"""
    from app.db.models import (
        ACCOUNT_TYPE_MEMBER,
        TENANT_KIND_MEMBER,
        TENANT_STATUS_ACTIVE,
    )
    from app.db.session import session_scope
    from app.services import provision_service, quota_service

    agent_id, _admin_id = await _agent_with_quota(db, "pm-agent", member=1)

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        result = await provision_service.open_member(
            session,
            db,
            actor=actor,
            username="pm-customer",
            days=30,
            modules=["carry", "monitor"],
        )

    account = result["account"]
    tenant = result["tenant"]
    assert account["account_type"] == ACCOUNT_TYPE_MEMBER
    assert account["role"] == "owner"
    assert account["parent_user_id"] == agent_id
    assert account["must_change_password"] is True
    assert account["tenant_id"] == tenant["id"]
    assert tenant["kind"] == TENANT_KIND_MEMBER
    assert tenant["status"] == TENANT_STATUS_ACTIVE
    assert tenant["owner_agent_id"] == agent_id
    assert tenant["quota_type"] == "member"
    assert tenant["quota_held"] is True
    assert result["modules"] == ["carry", "monitor"]
    assert result["limits"] == {}
    # 初始密码只在这一刻明文返回：平台统一下发 INITIAL_PASSWORD，首登必须自己改
    assert result["initial_password"] == INITIAL_PASSWORD

    async with session_scope() as session:
        assert await quota_service.balance_of(session, agent_id, "member") == 0
        _rows, total = await quota_service.list_ledger(
            session, subject_user_id=agent_id, action="open_member"
        )
    assert total == 1


async def test_open_member_expiry_is_end_of_last_local_day(db) -> None:
    """到期切点 = 第 N 天本地 23:59:59（默认 Asia/Shanghai）。"""
    from app.db.base import as_utc
    from app.db.session import session_scope
    from app.services import provision_service

    agent_id, _ = await _agent_with_quota(db, "pn-agent", member=1)

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        result = await provision_service.open_member(
            session, db, actor=actor, username="pn-customer", days=1, modules=["carry"]
        )

    assert as_utc(result["tenant"]["expires_at"]) == expiry_for_days(1)


async def test_open_member_without_quota_rolls_back_everything(db) -> None:
    """额度不足：整体回滚，不留半个账号，也不写流水。"""
    from sqlalchemy import func, select

    from app.db.models import User
    from app.db.session import session_scope
    from app.services import provision_service

    agent_id, _ = await _agent_with_quota(db, "po-agent", member=0)

    with pytest.raises(InsufficientQuotaError):
        async with session_scope() as session:
            actor = await _load(session, agent_id)
            await provision_service.open_member(
                session, db, actor=actor, username="po-customer", days=30, modules=["carry"]
            )

    async with session_scope() as session:
        exists = await session.scalar(
            select(func.count()).select_from(User).where(User.username == "po-customer")
        )
    assert exists == 0


async def test_platform_open_member_holds_no_quota(db) -> None:
    """平台开号不占额度：quota_type=none、quota_held=false、owner_agent_id 为空。"""
    from app.db.session import session_scope
    from app.services import provision_service, quota_service

    async with session_scope() as session:
        admin = await _make_user(session, db, "pp-admin")
        admin_id = admin.id

    async with session_scope() as session:
        actor = await _load(session, admin_id)
        result = await provision_service.open_member(
            session, db, actor=actor, username="pp-customer", days=30, modules=["carry"]
        )

    assert result["tenant"]["quota_type"] == "none"
    assert result["tenant"]["quota_held"] is False
    assert result["tenant"]["owner_agent_id"] is None
    assert result["account"]["parent_user_id"] is None

    async with session_scope() as session:
        assert await quota_service.get_quota(session, admin_id) is None


async def test_open_trial_is_fixed_one_day_with_minimal_limits(db) -> None:
    """试用固定 1 天，只给最小包（1 条线路、不允许导出）。"""
    from app.db.base import as_utc
    from app.db.session import session_scope
    from app.services import provision_service, quota_service, tenant_module_service

    agent_id, _ = await _agent_with_quota(db, "pt-agent", trial=2)

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        result = await provision_service.open_trial(
            session, db, actor=actor, username="pt-customer", template_code="trial_monitor"
        )

    assert result["modules"] == ["monitor"]
    assert result["limits"] == {"max_routes": 1, "allow_export": False}
    assert as_utc(result["tenant"]["expires_at"]) == expiry_for_days(1)
    assert result["tenant"]["quota_type"] == "trial"
    assert result["tenant"]["quota_held"] is True

    async with session_scope() as session:
        assert await quota_service.balance_of(session, agent_id, "trial") == 1
        limit = await tenant_module_service.get_limits(session, result["tenant"]["id"])
        assert limit is not None
        assert limit.max_routes == 1
        assert limit.allow_export is False
        assert await tenant_module_service.list_modules(session, result["tenant"]["id"]) == [
            "monitor"
        ]


async def test_open_trial_rejects_non_trial_template(db) -> None:
    """试用只能是 trial_carry / trial_monitor 两个模板，且天数不可传入。"""
    from app.db.session import session_scope
    from app.services import provision_service

    agent_id, _ = await _agent_with_quota(db, "pu-agent", trial=1)

    with pytest.raises(ValidationFailedError):
        async with session_scope() as session:
            actor = await _load(session, agent_id)
            await provision_service.open_trial(
                session, db, actor=actor, username="pu-customer", template_code="full"
            )


async def test_open_agent_uses_agent_quota_not_member_quota(db) -> None:
    """开下级代理只扣代理额度；顺手划拨落库是两笔。"""
    from app.db.session import session_scope
    from app.services import provision_service, quota_service

    agent_id, _ = await _agent_with_quota(db, "pa-agent", member=3, agent_quota=1)

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        result = await provision_service.open_agent(
            session,
            db,
            actor=actor,
            username="pa-child",
            allocate={"member": 2},
        )

    assert result["account"]["account_type"] == "agent"
    assert result["account"]["parent_user_id"] == agent_id
    assert result["allocated"] == {"member": 2}
    assert result["quota"]["member"] == 2

    async with session_scope() as session:
        assert await quota_service.balance_of(session, agent_id, "agent") == 0
        assert await quota_service.balance_of(session, agent_id, "member") == 1
        _rows, agent_ledger = await quota_service.list_ledger(
            session, subject_user_id=agent_id, action="open_agent"
        )
        _rows, alloc_ledger = await quota_service.list_ledger(
            session, subject_user_id=agent_id, action="allocate_out"
        )
    assert agent_ledger == 1
    assert alloc_ledger == 1


async def test_expire_release_is_idempotent(db) -> None:
    """到期释放把额度还给开设它的代理；重复调用不重复加。"""
    from app.db.session import session_scope
    from app.services import provision_service, quota_service, tenant_service

    agent_id, _ = await _agent_with_quota(db, "pe-agent", member=1)

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        result = await provision_service.open_member(
            session, db, actor=actor, username="pe-customer", days=1, modules=["carry"]
        )
        tenant_id = result["tenant"]["id"]

    async with session_scope() as session:
        tenant = await tenant_service.get_tenant(session, tenant_id)
        assert await provision_service.expire_release(session, tenant=tenant) is True

    async with session_scope() as session:
        assert await quota_service.balance_of(session, agent_id, "member") == 1
        tenant = await tenant_service.get_tenant(session, tenant_id)
        assert tenant.quota_held is False
        assert tenant.quota_type == "member"  # 保留类型便于追溯
        assert await provision_service.expire_release(session, tenant=tenant) is False

    async with session_scope() as session:
        assert await quota_service.balance_of(session, agent_id, "member") == 1
        _rows, total = await quota_service.list_ledger(
            session, subject_user_id=agent_id, action="expire_release"
        )
    assert total == 1


async def test_renew_extends_when_quota_still_held(db) -> None:
    """还没到期、额度仍握着：提前续费不重复占额度。"""
    from app.db.base import as_utc
    from app.db.session import session_scope
    from app.services import provision_service, quota_service, tenant_service

    agent_id, _ = await _agent_with_quota(db, "pr-agent", member=1)

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        result = await provision_service.open_member(
            session, db, actor=actor, username="pr-customer", days=1, modules=["carry"]
        )
        tenant_id = result["tenant"]["id"]

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        tenant = await tenant_service.get_tenant(session, tenant_id)
        await provision_service.renew(session, actor=actor, tenant=tenant, days=30)

    async with session_scope() as session:
        assert await quota_service.balance_of(session, agent_id, "member") == 0
        tenant = await tenant_service.get_tenant(session, tenant_id)
        assert tenant.quota_held is True
        assert as_utc(tenant.expires_at) == expiry_for_days(30)
        _rows, total = await quota_service.list_ledger(
            session, subject_user_id=agent_id, action="renew_consume"
        )
    assert total == 0


async def test_renew_recharges_after_expiry_released(db) -> None:
    """已到期且额度已释放：续期要重新扣 1 个会员额度。"""
    from app.db.session import session_scope
    from app.services import provision_service, quota_service, tenant_service

    agent_id, _ = await _agent_with_quota(db, "ps-agent", member=1)

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        result = await provision_service.open_member(
            session, db, actor=actor, username="ps-customer", days=1, modules=["carry"]
        )
        tenant_id = result["tenant"]["id"]

    async with session_scope() as session:
        tenant = await tenant_service.get_tenant(session, tenant_id)
        await provision_service.expire_release(session, tenant=tenant)

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        tenant = await tenant_service.get_tenant(session, tenant_id)
        await provision_service.renew(session, actor=actor, tenant=tenant, days=30)

    async with session_scope() as session:
        assert await quota_service.balance_of(session, agent_id, "member") == 0
        tenant = await tenant_service.get_tenant(session, tenant_id)
        assert tenant.quota_held is True
        _rows, total = await quota_service.list_ledger(
            session, subject_user_id=agent_id, action="renew_consume"
        )
    assert total == 1


async def test_renew_without_quota_and_not_held_fails(db) -> None:
    """额度已释放但又没钱续：报额度不足，不产生半截状态。"""
    from app.db.session import session_scope
    from app.services import provision_service, quota_service, tenant_service

    agent_id, admin_id = await _agent_with_quota(db, "pv-agent", member=1)

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        result = await provision_service.open_member(
            session, db, actor=actor, username="pv-customer", days=1, modules=["carry"]
        )
        tenant_id = result["tenant"]["id"]

    async with session_scope() as session:
        tenant = await tenant_service.get_tenant(session, tenant_id)
        await provision_service.expire_release(session, tenant=tenant)

    # 把释放回来的额度调走，制造"没钱续期"
    async with session_scope() as session:
        admin = await _load(session, admin_id)
        agent = await _load(session, agent_id)
        await quota_service.manual_adjust(
            session,
            actor=admin,
            target=agent,
            quota_type="member",
            delta=-1,
            note="测试挪走额度",
        )

    with pytest.raises(InsufficientQuotaError):
        async with session_scope() as session:
            actor = await _load(session, agent_id)
            tenant = await tenant_service.get_tenant(session, tenant_id)
            await provision_service.renew(session, actor=actor, tenant=tenant, days=30)


async def test_upgrade_trial_swaps_quota_and_clears_limits(db) -> None:
    """试用转正式：还试用额度、扣会员额度、解除试用限制，账号不动。"""
    from app.db.session import session_scope
    from app.services import provision_service, quota_service, tenant_module_service, tenant_service

    agent_id, _ = await _agent_with_quota(db, "pw-agent", member=1, trial=1)

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        result = await provision_service.open_trial(
            session, db, actor=actor, username="pw-customer", template_code="trial_carry"
        )
        tenant_id = result["tenant"]["id"]
        account_id = result["account"]["id"]

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        tenant = await tenant_service.get_tenant(session, tenant_id)
        upgraded = await provision_service.upgrade_trial(
            session, actor=actor, tenant=tenant, days=90, modules=["carry", "monitor"]
        )

    assert upgraded["tenant"]["quota_type"] == "member"
    assert upgraded["tenant"]["quota_held"] is True
    assert upgraded["tenant"]["expires_at"] == expiry_for_days(90)
    assert upgraded["modules"] == ["carry", "monitor"]

    async with session_scope() as session:
        assert await quota_service.balance_of(session, agent_id, "trial") == 1
        assert await quota_service.balance_of(session, agent_id, "member") == 0
        limit = await tenant_module_service.get_limits(session, tenant_id)
        assert limit.max_routes is None
        assert limit.allow_export is True
        account = await _load(session, account_id)
        assert account.tenant_id == tenant_id


async def test_upgrade_non_trial_is_rejected(db) -> None:
    """只有试用账号能转正式。"""
    from app.db.session import session_scope
    from app.services import provision_service, tenant_service

    agent_id, _ = await _agent_with_quota(db, "px-agent", member=2)

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        result = await provision_service.open_member(
            session, db, actor=actor, username="px-customer", days=30, modules=["carry"]
        )
        tenant_id = result["tenant"]["id"]

    with pytest.raises(ConflictError):
        async with session_scope() as session:
            actor = await _load(session, agent_id)
            tenant = await tenant_service.get_tenant(session, tenant_id)
            await provision_service.upgrade_trial(session, actor=actor, tenant=tenant, days=30)


async def test_unknown_template_is_rejected(db) -> None:
    """模板不存在/停用时开号直接失败。"""
    from app.db.session import session_scope
    from app.services import provision_service

    admin_id = None
    async with session_scope() as session:
        admin = await _make_user(session, db, "py-admin")
        admin_id = admin.id

    with pytest.raises(NotFoundError):
        async with session_scope() as session:
            actor = await _load(session, admin_id)
            await provision_service.open_member(
                session, db, actor=actor, username="py-customer", days=30, template_code="nope"
            )


def test_initial_password_is_the_platform_default() -> None:
    """开号统一下发 a123456；只有显式传密码才走强度校验。"""
    from app.services.provision_service import resolve_initial_password

    assert INITIAL_PASSWORD == "a123456"
    assert resolve_initial_password(None) == (INITIAL_PASSWORD, True)
    assert resolve_initial_password("   ") == (INITIAL_PASSWORD, True)
    assert resolve_initial_password(" Custom12345 ") == ("Custom12345", False)


def test_expiry_helper_matches_design() -> None:
    """1 天 = 当天 23:59:59；N 天 = 第 N 天 23:59:59。"""
    # 固定起算日，避免用例跟着"今天"漂
    start = datetime(2026, 9, 27, tzinfo=UTC).date()
    one_day = expiry_for_days(1, start=start)
    later = expiry_for_days(3, start=start)
    assert later - one_day == timedelta(days=2)
    # 缺省 start 就是"今天"：1 天的到期时刻 = 今天 23:59:59
    today = datetime.now(resolve_timezone(None)).date()
    assert expiry_for_days(1) == expiry_for_days(1, start=today)


async def test_open_member_without_plan_defaults_to_full(db) -> None:
    """不勾功能块也不选模板 = 全功能：开出来就能用搬运 / 监听 / 资源发现，不会再有空白账号。"""
    from app.db.session import session_scope
    from app.services import provision_service, quota_service

    agent_id, _admin_id = await _agent_with_quota(db, "pz-agent", member=2)

    for index, empty in enumerate((None, []), start=1):
        async with session_scope() as session:
            actor = await _load(session, agent_id)
            result = await provision_service.open_member(
                session,
                db,
                actor=actor,
                username=f"pz-customer{index}",
                days=30,
                modules=empty,
            )
        assert result["modules"] == ["carry", "discovery", "monitor"]
        assert result["module_labels"] == ["搬运帖子", "资源发现", "监听会员"]

    # 两个号都真开了，代理额度也按规矩各扣 1 个
    async with session_scope() as session:
        balance = await quota_service.balance_of(session, agent_id, "member")
    assert balance == 0


async def test_standard_plan_and_empty_template_grant_full(db) -> None:
    """「常规开通」= 全功能；就算有人手工建了个没功能块的模板，也开不出空白会员。"""
    from sqlalchemy import select

    from app.db.session import session_scope
    from app.services import provision_service, tenant_module_service

    agent_id, _admin_id = await _agent_with_quota(db, "pz-agent2", member=3)

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        standard = await provision_service.open_member(
            session,
            db,
            actor=actor,
            username="pz-standard",
            days=30,
            template_code="standard",
        )
    assert standard["modules"] == ["carry", "discovery", "monitor"]

    # 手工塞一个「没有任何功能块」的模板，用来验证兜底
    async with session_scope() as session:
        await tenant_module_service.create_plan_template(
            session,
            code="blank-plan",
            name="空模板",
            modules=[],
        )
        await session.commit()

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        fallback = await provision_service.open_member(
            session,
            db,
            actor=actor,
            username="pz-blank-template",
            days=30,
            template_code="blank-plan",
        )
    assert fallback["modules"] == ["carry", "discovery", "monitor"]

    # 但「改功能包」选空模板要直接报错，不许把老客户功能清空
    async with session_scope() as session:
        from app.db.models import Tenant, User

        user = await session.scalar(select(User).where(User.username == "pz-standard"))
        tenant = await session.get(Tenant, user.tenant_id)
        actor = await _load(session, agent_id)
        with pytest.raises(ValidationFailedError) as excinfo:
            await provision_service.set_plan(
                session,
                actor=actor,
                tenant=tenant,
                template_code="blank-plan",
            )
        assert "至少选择一个功能块" in str(excinfo.value)
