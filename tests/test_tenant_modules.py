"""P2 账号体系：功能包模板、功能授权、用量限制与功能守卫。"""

from __future__ import annotations

import pytest
from conftest import auth_header, login

BUSINESS_PATHS = ("/api/accounts", "/api/routes", "/api/system/status")


async def _make_account(config, session_factory, **kwargs):
    """在同一个临时库里建账号，返回登录令牌。"""
    async with session_factory() as session:
        from app.services import user_service

        return await user_service.create_user(session, config, **kwargs)


# --------------------------------------------------------------------------- #
# 服务层：模板与授权
# --------------------------------------------------------------------------- #
async def test_plan_templates_are_seeded(db) -> None:
    from app.db.session import session_scope
    from app.services import tenant_module_service

    async with session_scope() as session:
        codes = [item.code for item in await tenant_module_service.list_plan_templates(session)]

    assert codes == ["trial_carry", "trial_monitor", "standard", "full"]


async def test_seed_plan_templates_is_idempotent(db) -> None:
    from app.db.session import session_scope
    from app.services import tenant_module_service

    async with session_scope() as session:
        created = await tenant_module_service.seed_plan_templates(session)
        total = await tenant_module_service.list_plan_templates(session, enabled_only=False)

    assert created == 0
    assert len(total) == 4


async def test_set_modules_toggles_and_keeps_rows(db) -> None:
    from app.db.session import session_scope
    from app.services import tenant_module_service, tenant_service, user_service

    async with session_scope() as session:
        owner = await user_service.create_user(
            session, db, username="mod-u1", password="Pass1234", account_type="member"
        )
        tenant = await tenant_service.create_tenant(session, name="甲", owner_user_id=owner.id)

    async with session_scope() as session:
        enabled = await tenant_module_service.set_modules(
            session,
            tenant_id=tenant.id,
            modules=["carry", "monitor"],
            granted_by="admin",
        )
    assert enabled == ["carry", "monitor"]

    async with session_scope() as session:
        assert await tenant_module_service.has_module(session, tenant.id, "carry") is True
        assert await tenant_module_service.has_module(session, tenant.id, "discovery") is False
        await tenant_module_service.revoke_module(session, tenant_id=tenant.id, module="carry")

    async with session_scope() as session:
        # 关掉只置 false，行还在（可追溯"曾经开过"）
        rows = await tenant_module_service.list_module_rows(session, tenant.id)
        assert sorted(row.module for row in rows) == ["carry", "monitor"]
        assert await tenant_module_service.list_modules(session, tenant.id) == ["monitor"]


async def test_grant_module_rejects_unknown_code(db) -> None:
    from app.core.errors import ValidationFailedError
    from app.db.session import session_scope
    from app.services import tenant_module_service

    async with session_scope() as session:
        with pytest.raises(ValidationFailedError):
            await tenant_module_service.grant_module(session, tenant_id=1, module="unknown")


async def test_limits_payload_hides_unrestricted_keys(db) -> None:
    from app.db.session import session_scope
    from app.services import tenant_module_service, tenant_service, user_service

    async with session_scope() as session:
        owner = await user_service.create_user(
            session, db, username="lim-u1", password="Pass1234", account_type="member"
        )
        tenant = await tenant_service.create_tenant(session, name="乙", owner_user_id=owner.id)

    async with session_scope() as session:
        assert await tenant_module_service.get_limits(session, tenant.id) is None
        assert tenant_module_service.limits_to_payload(None) == {}

    async with session_scope() as session:
        row = await tenant_module_service.set_limits(
            session, tenant_id=tenant.id, max_routes=1, allow_export=False
        )
        payload = tenant_module_service.limits_to_payload(row)

    # 数值为 None 的键不出现；allow_export 只在关闭时出现
    assert payload == {"max_routes": 1, "allow_export": False}


# --------------------------------------------------------------------------- #
# 服务层：账号创建
# --------------------------------------------------------------------------- #
async def test_member_role_is_forced_to_owner(db) -> None:
    from app.db.session import session_scope
    from app.services import user_service

    async with session_scope() as session:
        user = await user_service.create_user(
            session,
            db,
            username="member1",
            password="Pass1234",
            account_type="member",
            role="super_admin",
        )

    assert user.account_type == "member"
    assert user.role == "owner"


async def test_agent_account_records_parent(db) -> None:
    from app.db.session import session_scope
    from app.services import user_service

    async with session_scope() as session:
        agent = await user_service.create_user(
            session,
            db,
            username="agent1",
            password="Pass1234",
            account_type="agent",
            role="sub_admin",
        )
        sub = await user_service.create_user(
            session,
            db,
            username="agent2",
            password="Pass1234",
            account_type="agent",
            parent_user_id=agent.id,
        )

    assert sub.parent_user_id == agent.id


async def test_create_user_rejects_unknown_parent(db) -> None:
    from app.core.errors import NotFoundError
    from app.db.session import session_scope
    from app.services import user_service

    async with session_scope() as session:
        with pytest.raises(NotFoundError):
            await user_service.create_user(
                session,
                db,
                username="agent3",
                password="Pass1234",
                account_type="agent",
                parent_user_id=9999,
            )


# --------------------------------------------------------------------------- #
# 接口层：身份契约与功能守卫
# --------------------------------------------------------------------------- #
async def _provision(config, *, username, account_type, modules, expires_at=None):
    """建账号 + 建租户 + 授权，返回账号 ID。"""
    from app.db.session import session_scope
    from app.services import tenant_module_service, tenant_service, user_service

    async with session_scope() as session:
        user = await user_service.create_user(
            session,
            config,
            username=username,
            password="Pass1234",
            account_type=account_type,
            must_change_password=False,
        )
        if account_type != "member":
            return user.id
        tenant = await tenant_service.create_tenant(
            session, name=f"{username}-租户", owner_user_id=user.id, expires_at=expires_at
        )
        user.tenant_id = tenant.id
        await session.commit()
        if modules:
            await tenant_module_service.set_modules(
                session, tenant_id=tenant.id, modules=modules, granted_by="admin"
            )
        return user.id


async def test_member_login_returns_module_contract(admin_client, api_config) -> None:
    from datetime import UTC, datetime

    expires = datetime(2026, 10, 1, 23, 59, 59, tzinfo=UTC)
    await _provision(
        api_config,
        username="memberA",
        account_type="member",
        modules=["carry"],
        expires_at=expires,
    )

    response = await login(admin_client, username="memberA", password="Pass1234")
    body = response.json()

    assert response.status_code == 200
    assert body["role"] == "owner"
    assert body["account_type"] == "member"
    assert body["modules"] == ["carry"]
    assert body["tenant_status"] == "active"
    assert body["expires_at"].startswith("2026-10-01T23:59:59")
    assert body["limits"] == {}
    assert body["tenant_id"] is not None


async def test_member_module_guard_blocks_other_modules(admin_client, api_config) -> None:
    await _provision(api_config, username="memberB", account_type="member", modules=["carry"])
    token = (await login(admin_client, username="memberB", password="Pass1234")).json()["token"]
    headers = auth_header(token)

    # 基础能力恒开
    assert (await admin_client.get("/api/accounts", headers=headers)).status_code == 200
    # carry 放行
    assert (await admin_client.get("/api/ad-assets", headers=headers)).status_code == 200
    # monitor / discovery 一律 403（不是"看不到菜单"而已）
    assert (await admin_client.get("/api/leads", headers=headers)).status_code == 403
    assert (await admin_client.get("/api/keyword-groups", headers=headers)).status_code == 403
    assert (await admin_client.get("/api/resources", headers=headers)).status_code == 403
    # 平台内部接口不放给会员
    assert (await admin_client.get("/api/users", headers=headers)).status_code == 403


async def test_agent_is_blocked_from_every_business_api(admin_client, api_config) -> None:
    await _provision(api_config, username="agentC", account_type="agent", modules=[])
    token = (await login(admin_client, username="agentC", password="Pass1234")).json()["token"]
    headers = auth_header(token)

    for path in BUSINESS_PATHS:
        assert (await admin_client.get(path, headers=headers)).status_code == 403
    assert (await admin_client.get("/api/ad-assets", headers=headers)).status_code == 403
    assert (await admin_client.get("/api/users", headers=headers)).status_code == 403


async def test_agent_login_contract_has_no_modules(admin_client, api_config) -> None:
    await _provision(api_config, username="agentD", account_type="agent", modules=[])
    body = (await login(admin_client, username="agentD", password="Pass1234")).json()

    assert body["account_type"] == "agent"
    assert body["modules"] == []
    assert body["tenant_id"] is None


async def test_member_cannot_see_platform_account_counts(admin_client, api_config) -> None:
    """平台级账号统计只给平台账号：会员看到的应为 null，避免跨租户信息泄露。"""
    await _provision(api_config, username="memberD", account_type="member", modules=["carry"])
    member_token = (await login(admin_client, username="memberD", password="Pass1234")).json()[
        "token"
    ]

    member_status = (
        await admin_client.get("/api/system/status", headers=auth_header(member_token))
    ).json()
    assert member_status["counts"]["users"] is None
    assert member_status["counts"]["active_super_admins"] is None

    admin_token = (await login(admin_client)).json()["token"]
    admin_status = (
        await admin_client.get("/api/system/status", headers=auth_header(admin_token))
    ).json()
    assert isinstance(admin_status["counts"]["users"], int)


async def test_check_endpoint_matches_login_contract(admin_client, api_config) -> None:
    await _provision(api_config, username="memberC", account_type="member", modules=["monitor"])
    login_body = (await login(admin_client, username="memberC", password="Pass1234")).json()

    checked = (
        await admin_client.get("/api/auth/check", headers=auth_header(login_body["token"]))
    ).json()

    assert checked["authenticated"] is True
    for key in (
        "username",
        "role",
        "account_type",
        "tenant_id",
        "tenant_status",
        "modules",
        "limits",
    ):
        assert checked[key] == login_body[key]
