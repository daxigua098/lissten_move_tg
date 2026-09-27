"""P4-02：账号过期 / 停用后的服务端守卫（只读放行、写操作 403、白名单放行）。"""

from __future__ import annotations

from datetime import timedelta

from conftest import auth_header, login

from app.db.base import utc_now
from app.db.session import session_scope


async def _make_member(
    api_config,
    username: str,
    *,
    expires_at=None,
    modules: tuple[str, ...] = ("carry", "monitor"),
) -> tuple[int, int]:
    """建一个会员账号 + 租户（免强制改密），返回（user_id, tenant_id）。"""
    from app.services import tenant_module_service, tenant_service, user_service

    async with session_scope() as session:
        user = await user_service.create_user(
            session,
            api_config,
            username=username,
            password="MemberPass123",
            account_type="member",
            must_change_password=False,
        )
        tenant = await tenant_service.create_tenant(
            session,
            name=f"t-{username}",
            owner_user_id=user.id,
            expires_at=expires_at,
        )
        tenant.runtime_enabled = True
        # 会员账号与租户是互相指认的两条外键，开号链路里由 provision_service 回填，
        # 这里手工建号也必须补上，否则身份的 tenant_id 是空的
        user.tenant_id = tenant.id
        await session.commit()
        user_id, tenant_id = user.id, tenant.id

    async with session_scope() as session:
        await tenant_module_service.set_modules(
            session,
            tenant_id=tenant_id,
            modules=list(modules),
            granted_by="test",
        )
    return user_id, tenant_id


async def _update_tenant(tenant_id: int, **values) -> None:
    from app.db.models import Tenant

    async with session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        for key, value in values.items():
            setattr(tenant, key, value)
        await session.commit()


async def _member_token(client, username: str = "p4-api") -> str:
    response = await login(client, username, "MemberPass123")
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def test_expired_member_is_read_only(admin_client, api_config) -> None:
    """过期会员：GET 全部放行，业务写操作 403，白名单（auth）照旧可用。"""
    _user_id, tenant_id = await _make_member(api_config, "p4-api")
    token = await _member_token(admin_client)

    active = await admin_client.get("/api/system/status", headers=auth_header(token))
    assert active.status_code == 200, active.text
    assert active.json()["tenant"]["status"] == "active"

    # 到期：把 expires_at 推到过去（模拟"心跳还没跑到，但时间已经过了"）
    await _update_tenant(tenant_id, expires_at=utc_now() - timedelta(seconds=1))

    readable = await admin_client.get("/api/system/status", headers=auth_header(token))
    assert readable.status_code == 200, readable.text
    body = readable.json()
    assert body["tenant"]["status"] == "expired"
    assert body["tenant"]["stop_reason_label"] == ""  # 巡检还没跑，只是判定为过期

    checked = await admin_client.get("/api/auth/check", headers=auth_header(token))
    assert checked.status_code == 200
    assert checked.json()["tenant_status"] == "expired"

    blocked = await admin_client.post("/api/runtime/stop", headers=auth_header(token))
    assert blocked.status_code == 403, blocked.text
    detail = blocked.json()
    assert detail["code"] == "TENANT_INACTIVE"
    assert "续费" in detail["detail"]
    assert detail["tenant_status"] == "expired"

    # 过期后也不能自己启动（要先续费）
    start = await admin_client.post("/api/runtime/start", headers=auth_header(token))
    assert start.status_code == 403, start.text

    # 导出已有数据仍然可用（需求：过期后可看、可查、可导出）
    exported = await admin_client.get("/api/leads/export.csv", headers=auth_header(token))
    assert exported.status_code == 200, exported.text

    # 白名单：登录 / 改密 / 登出必须放行，否则用户进不来也出不去
    logout = await admin_client.post("/api/auth/logout", headers=auth_header(token))
    assert logout.status_code == 200, logout.text


async def test_renewed_member_can_write_again(admin_client, api_config) -> None:
    """续期后写操作立刻恢复（不用重启，判定是现算的）。"""
    _user_id, tenant_id = await _make_member(api_config, "p4-renew")
    token = await _member_token(admin_client, "p4-renew")

    await _update_tenant(tenant_id, expires_at=utc_now() - timedelta(days=1))
    blocked = await admin_client.post("/api/runtime/stop", headers=auth_header(token))
    assert blocked.status_code == 403

    await _update_tenant(tenant_id, expires_at=utc_now() + timedelta(days=30))
    allowed = await admin_client.post("/api/runtime/stop", headers=auth_header(token))
    assert allowed.status_code == 200, allowed.text
    body = allowed.json()
    assert body["tenant"]["runtime_enabled"] is False
    assert body["tenant"]["runtime_stop_reason"] == "manual"

    # 再启动回来：会员的启动是"租户开关"，顺手把进程也拉起来（没在跑就启动）
    started = await admin_client.post("/api/runtime/start", headers=auth_header(token))
    assert started.status_code == 200, started.text
    assert started.json()["tenant"]["runtime_enabled"] is True


async def test_suspended_member_is_read_only_too(admin_client, api_config) -> None:
    """停用与过期同样拦写操作（停用优先于过期）。"""
    _user_id, tenant_id = await _make_member(api_config, "p4-susp")
    token = await _member_token(admin_client, "p4-susp")

    await _update_tenant(
        tenant_id,
        status="suspended",
        expires_at=utc_now() - timedelta(days=1),
    )

    readable = await admin_client.get("/api/system/status", headers=auth_header(token))
    assert readable.status_code == 200
    assert readable.json()["tenant"]["status"] == "suspended"

    blocked = await admin_client.post("/api/runtime/stop", headers=auth_header(token))
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["tenant_status"] == "suspended"


async def test_platform_is_not_affected_by_tenant_guard(admin_client, api_config) -> None:
    """平台账号不属于任何租户，租户停了也能继续操作（含自营租户被停的极端情况）。"""
    platform = (await login(admin_client)).json()["token"]

    await _update_tenant(1, status="suspended")

    ok = await admin_client.post("/api/runtime/pause", headers=auth_header(platform))
    assert ok.status_code == 200, ok.text
    resume = await admin_client.post("/api/runtime/resume", headers=auth_header(platform))
    assert resume.status_code == 200, resume.text
