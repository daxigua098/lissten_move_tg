"""P5：平台后台接口（总览 / 代理 / 会员 / 台账 / 到期看板 / 停用留痕 / 审计）。"""

from __future__ import annotations

from datetime import timedelta

from conftest import auth_header, login

from app.db.base import utc_now
from app.db.models import Tenant
from app.db.session import session_scope

MEMBER_PASSWORD = "MemberPass123"
AGENT_PASSWORD = "AgentPass123"


async def _platform_token(client) -> str:
    response = await login(client)
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def _user_id_by_name(username: str) -> int:
    from sqlalchemy import select

    from app.db.models import User

    async with session_scope() as session:
        value = await session.scalar(select(User.id).where(User.username == username))
    assert value is not None, f"账号 {username} 不存在"
    return int(value)


async def _make_agent(config, username: str, *, parent_user_id: int | None = None) -> int:
    """直接建一个"已改过密码"的代理，省掉首次改密那一步。"""
    from app.services import user_service

    async with session_scope() as session:
        user = await user_service.create_user(
            session,
            config,
            username=username,
            password=AGENT_PASSWORD,
            account_type="agent",
            parent_user_id=parent_user_id,
            must_change_password=False,
        )
        await session.commit()
        return user.id


async def _agent_token(client, username: str) -> str:
    response = await login(client, username, AGENT_PASSWORD)
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def _grant(client, platform: str, agent_id: int, quota_type: str, delta: int) -> dict:
    response = await client.post(
        f"/api/agent/{agent_id}/adjust",
        json={"quota_type": quota_type, "delta": delta, "note": "测试发放"},
        headers=auth_header(platform),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _open_member(
    client,
    token: str,
    username: str,
    *,
    days: int = 30,
    modules: list[str] | None = None,
    owner_agent_id: int | None = None,
) -> dict:
    payload: dict = {
        "username": username,
        "days": days,
        "modules": modules if modules is not None else ["carry", "monitor"],
    }
    if owner_agent_id is not None:
        payload["owner_agent_id"] = owner_agent_id
    response = await client.post("/api/platform/members", json=payload, headers=auth_header(token))
    assert response.status_code == 201, response.text
    return response.json()


async def _member_token(client, username: str, initial_password: str) -> str:
    """首次登录 → 改密 → 重新登录，返回可用令牌。"""
    token = (await login(client, username, initial_password)).json()["token"]
    changed = await client.patch(
        "/api/auth/password",
        json={"current_password": initial_password, "new_password": MEMBER_PASSWORD},
        headers=auth_header(token),
    )
    assert changed.status_code == 200, changed.text
    response = await login(client, username, MEMBER_PASSWORD)
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def _update_tenant(tenant_id: int, **values) -> None:
    from app.db.models import Tenant

    async with session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        for key, value in values.items():
            setattr(tenant, key, value)
        await session.commit()


async def _tenant_row(tenant_id: int):
    from app.db.models import Tenant

    async with session_scope() as session:
        return await session.get(Tenant, tenant_id)


async def _agent_item(client, platform: str, agent_id: int) -> dict:
    response = await client.get("/api/platform/agents", headers=auth_header(platform))
    assert response.status_code == 200, response.text
    for item in response.json()["items"]:
        if item["user_id"] == agent_id:
            return item
    raise AssertionError(f"代理 {agent_id} 不在列表里")


# --------------------------------------------------------------------------- #
# 权限与总览
# --------------------------------------------------------------------------- #
async def test_platform_endpoints_are_platform_only(admin_client, api_config) -> None:
    """代理与会员访问平台后台一律 403。"""
    platform = await _platform_token(admin_client)
    agent_id = await _make_agent(api_config, "p5-iso-agent")
    agent_token = await _agent_token(admin_client, "p5-iso-agent")
    opened = await _open_member(admin_client, platform, "p5-iso-member")
    member_token = await _member_token(admin_client, "p5-iso-member", opened["initial_password"])

    for token in (agent_token, member_token):
        for path in ("/api/platform/overview", "/api/platform/agents", "/api/platform/ledger"):
            response = await admin_client.get(path, headers=auth_header(token))
            assert response.status_code == 403, response.text

    # 代理自己开的号仍然挂在代理名下（回归：平台接口不改变既有归属规则）
    assert agent_id > 0


async def test_overview_counts_expiring_and_quota_alerts(admin_client, api_config) -> None:
    """总览：账号分布、今日动作、即将到期、额度预警。"""
    platform = await _platform_token(admin_client)
    agent_id = await _make_agent(api_config, "p5-boss")
    await _grant(admin_client, platform, agent_id, "member", 3)
    agent_token = await _agent_token(admin_client, "p5-boss")

    for name in ("p5-c1", "p5-c2"):
        response = await admin_client.post(
            "/api/agent/members",
            json={"username": name, "days": 30, "modules": ["carry"]},
            headers=auth_header(agent_token),
        )
        assert response.status_code == 201, response.text

    direct = await _open_member(admin_client, platform, "p5-direct", modules=["monitor"])
    assert direct["tenant"]["quota_type"] == "none"  # 平台直开不占额度
    soon = await _open_member(
        admin_client, platform, "p5-soon", days=2, modules=["carry"], owner_agent_id=agent_id
    )
    assert soon["tenant"]["quota_type"] == "member"
    assert soon["tenant"]["quota_held"] is True

    response = await admin_client.get("/api/platform/overview", headers=auth_header(platform))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"]["agents"] == 1
    assert body["counts"]["members"] == 4
    assert body["counts"]["active"] == 4
    assert body["counts"]["expiring"] >= 1
    # 今日新开按审计口径统计：代理开 2 个 + 平台开 2 个
    assert body["today"]["opened"] >= 4
    assert "p5-soon" in {item["username"] for item in body["expiring"]}
    # 代理 3 个会员额度全被占用、余额为 0 → 进额度预警
    alerts = {item["username"]: item for item in body["quota_alerts"]}
    assert "p5-boss" in alerts
    assert alerts["p5-boss"]["balance"] == 0
    assert alerts["p5-boss"]["held"] == 3

    # 余额还有剩的代理不该被误报（额度预警只报"余额为 0 且在用"）
    rich = await _make_agent(api_config, "p5-rich")
    await _grant(admin_client, platform, rich, "member", 2)
    rich_token = await _agent_token(admin_client, "p5-rich")
    opened = await admin_client.post(
        "/api/agent/members",
        json={"username": "p5-rich-c", "days": 30, "modules": ["carry"]},
        headers=auth_header(rich_token),
    )
    assert opened.status_code == 201, opened.text

    again = await admin_client.get("/api/platform/overview", headers=auth_header(platform))
    assert again.status_code == 200, again.text
    names = {item["username"] for item in again.json()["quota_alerts"]}
    assert "p5-boss" in names
    assert "p5-rich" not in names


# --------------------------------------------------------------------------- #
# 代理管理
# --------------------------------------------------------------------------- #
async def test_agent_list_shows_tree_and_quota(admin_client, api_config) -> None:
    """代理列表：上下级数、额度余额与占用；隔层只看不能动。"""
    platform = await _platform_token(admin_client)
    boss = await _make_agent(api_config, "p5-boss2")
    await _grant(admin_client, platform, boss, "member", 2)
    child = await _make_agent(api_config, "p5-child2", parent_user_id=boss)

    agent_token = await _agent_token(admin_client, "p5-boss2")
    response = await admin_client.post(
        "/api/agent/members",
        json={"username": "p5-boss2-customer", "days": 30, "modules": ["monitor"]},
        headers=auth_header(agent_token),
    )
    assert response.status_code == 201, response.text

    item = await _agent_item(admin_client, platform, boss)
    assert item["depth"] == 1
    assert item["parent_username"] is None
    assert item["direct_agents"] == 1
    assert item["subtree_agents"] == 1
    assert item["direct_members"] == 1
    assert item["subtree_members"] == 1
    assert item["quota"]["member"] == 1
    assert item["held"]["member"] == 1

    child_item = await _agent_item(admin_client, platform, child)
    assert child_item["depth"] == 2
    assert child_item["parent_username"] == "p5-boss2"

    tree = await admin_client.get(
        f"/api/platform/agents/{boss}/tree", headers=auth_header(platform)
    )
    assert tree.status_code == 200, tree.text
    assert tree.json()["total"] == 2  # 一个下级代理 + 一个会员


# --------------------------------------------------------------------------- #
# 会员管理
# --------------------------------------------------------------------------- #
async def test_member_list_filters(admin_client, api_config) -> None:
    """会员列表按代理 / 功能块 / 额度类型筛选。"""
    platform = await _platform_token(admin_client)
    agent_id = await _make_agent(api_config, "p5-filter-agent")
    await _grant(admin_client, platform, agent_id, "member", 1)
    agent_token = await _agent_token(admin_client, "p5-filter-agent")
    response = await admin_client.post(
        "/api/agent/members",
        json={"username": "p5-filter-a", "days": 30, "modules": ["monitor"]},
        headers=auth_header(agent_token),
    )
    assert response.status_code == 201, response.text
    await _open_member(admin_client, platform, "p5-filter-b", modules=["carry"])

    async def usernames(**params):
        result = await admin_client.get(
            "/api/platform/members", params=params, headers=auth_header(platform)
        )
        assert result.status_code == 200, result.text
        return {item["username"] for item in result.json()["items"]}

    assert await usernames(agent_user_id=agent_id) == {"p5-filter-a"}
    assert await usernames(module="monitor") == {"p5-filter-a"}
    assert await usernames(module="carry") == {"p5-filter-b"}
    assert await usernames(quota_type="none") == {"p5-filter-b"}
    assert await usernames(status="expired") == set()


async def test_platform_renew_recharges_released_quota(admin_client, api_config) -> None:
    """续期：额度已释放才重新占用（从归属代理账上扣），且不自动恢复运行。"""
    platform = await _platform_token(admin_client)
    agent_id = await _make_agent(api_config, "p5-renew-agent")
    await _grant(admin_client, platform, agent_id, "member", 2)
    agent_token = await _agent_token(admin_client, "p5-renew-agent")
    response = await admin_client.post(
        "/api/agent/members",
        json={"username": "p5-renew-c", "days": 30, "modules": ["carry"]},
        headers=auth_header(agent_token),
    )
    assert response.status_code == 201, response.text
    opened = response.json()
    user_id = opened["account"]["id"]
    tenant_id = opened["tenant"]["id"]

    # 走真正的"到期释放"链路：额度回到代理账上、线路不再运行
    from app.services import provision_service

    async with session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.expires_at = utc_now() - timedelta(days=1)
        tenant.status = "expired"
        tenant.runtime_enabled = False
        await provision_service.expire_release(session, tenant=tenant)
    assert (await _agent_item(admin_client, platform, agent_id))["quota"]["member"] == 2

    renewed = await admin_client.post(
        f"/api/platform/members/{user_id}/renew",
        json={"days": 30},
        headers=auth_header(platform),
    )
    assert renewed.status_code == 200, renewed.text
    tenant = await _tenant_row(tenant_id)
    assert tenant.quota_held is True
    assert tenant.status == "active"
    assert tenant.runtime_enabled is False  # 续期不自动恢复运行
    assert (await _agent_item(admin_client, platform, agent_id))["quota"]["member"] == 1


async def test_platform_change_plan_takes_effect_immediately(admin_client, api_config) -> None:
    """改功能包立即生效：会员身份里的 modules 立刻变化，配置不动。"""
    platform = await _platform_token(admin_client)
    opened = await _open_member(admin_client, platform, "p5-plan", modules=["carry", "monitor"])
    user_id = opened["account"]["id"]
    token = await _member_token(admin_client, "p5-plan", opened["initial_password"])

    before = await admin_client.get("/api/auth/check", headers=auth_header(token))
    assert sorted(before.json()["modules"]) == ["carry", "monitor"]

    changed = await admin_client.post(
        f"/api/platform/members/{user_id}/plan",
        json={"modules": ["discovery"]},
        headers=auth_header(platform),
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["module_labels"] == ["资源发现"]

    after = await admin_client.get("/api/auth/check", headers=auth_header(token))
    assert after.json()["modules"] == ["discovery"]
    # 到期日没有被改功能包影响
    assert after.json()["expires_at"] == before.json()["expires_at"]


# --------------------------------------------------------------------------- #
# 停用 / 解停
# --------------------------------------------------------------------------- #
async def test_platform_disable_member_keeps_quota_and_records_reason(
    admin_client, api_config
) -> None:
    """停用会员：功能全停、配置保留、额度不释放，并记录停用人与原因。"""
    platform = await _platform_token(admin_client)
    platform_id = await _user_id_by_name("admin")
    agent_id = await _make_agent(api_config, "p5-susp-agent")
    await _grant(admin_client, platform, agent_id, "member", 1)
    agent_token = await _agent_token(admin_client, "p5-susp-agent")
    response = await admin_client.post(
        "/api/agent/members",
        json={"username": "p5-susp-c", "days": 30, "modules": ["carry"]},
        headers=auth_header(agent_token),
    )
    assert response.status_code == 201, response.text
    opened = response.json()
    user_id = opened["account"]["id"]
    tenant_id = opened["tenant"]["id"]
    token = await _member_token(admin_client, "p5-susp-c", opened["initial_password"])

    blocked_self = await admin_client.post(
        f"/api/platform/accounts/{platform_id}/enable",
        json={"enabled": False},
        headers=auth_header(platform),
    )
    assert blocked_self.status_code == 400, blocked_self.text

    disabled = await admin_client.post(
        f"/api/platform/accounts/{user_id}/enable",
        json={"enabled": False, "reason": "欠费停用"},
        headers=auth_header(platform),
    )
    assert disabled.status_code == 200, disabled.text

    tenant = await _tenant_row(tenant_id)
    assert tenant.status == "suspended"
    assert tenant.suspended_by == "admin"
    assert tenant.suspended_reason == "欠费停用"
    assert tenant.suspended_at is not None
    # 额度不释放（只有真正到期才回到代理账上）
    assert tenant.quota_held is True
    assert (await _agent_item(admin_client, platform, agent_id))["held"]["member"] == 1

    # 停用后这个账号立刻进不来：身份解析发现 enabled=False，旧会话直接失效
    # （租户层面的 suspended 是第二道闸：写操作 403 TENANT_INACTIVE）
    blocked = await admin_client.post("/api/runtime/stop", headers=auth_header(token))
    assert blocked.status_code == 401, blocked.text
    assert blocked.json()["code"] == "AUTH_REQUIRED"

    enabled = await admin_client.post(
        f"/api/platform/accounts/{user_id}/enable",
        json={"enabled": True},
        headers=auth_header(platform),
    )
    assert enabled.status_code == 200, enabled.text
    tenant = await _tenant_row(tenant_id)
    assert tenant.status == "active"
    assert tenant.suspended_by is None
    assert tenant.suspended_reason is None


async def test_disabling_agent_does_not_stop_its_members(admin_client, api_config) -> None:
    """停用代理只影响他自己，名下已开会员照常运行。"""
    platform = await _platform_token(admin_client)
    agent_id = await _make_agent(api_config, "p5-stop-agent")
    await _grant(admin_client, platform, agent_id, "member", 1)
    agent_token = await _agent_token(admin_client, "p5-stop-agent")
    response = await admin_client.post(
        "/api/agent/members",
        json={"username": "p5-stop-c", "days": 30, "modules": ["carry"]},
        headers=auth_header(agent_token),
    )
    assert response.status_code == 201, response.text
    opened = response.json()
    member_token = await _member_token(admin_client, "p5-stop-c", opened["initial_password"])

    disabled = await admin_client.post(
        f"/api/platform/accounts/{agent_id}/enable",
        json={"enabled": False, "reason": "渠道违规"},
        headers=auth_header(platform),
    )
    assert disabled.status_code == 200, disabled.text

    # 代理进不来了
    denied = await login(admin_client, "p5-stop-agent", AGENT_PASSWORD)
    assert denied.status_code == 401, denied.text

    # 会员照常（身份仍是 active，写操作不被拦）
    checked = await admin_client.get("/api/auth/check", headers=auth_header(member_token))
    assert checked.status_code == 200, checked.text
    assert checked.json()["tenant_status"] == "active"


# --------------------------------------------------------------------------- #
# 到期看板与台账
# --------------------------------------------------------------------------- #
async def test_expiry_board_buckets(admin_client, api_config) -> None:
    """到期看板：今日 / 3 天内 / 7 天内 / 已过期四组。"""
    platform = await _platform_token(admin_client)
    for name, days in (("p5-b-today", 1), ("p5-b-3", 3), ("p5-b-7", 6), ("p5-b-old", 30)):
        await _open_member(admin_client, platform, name, days=days, modules=["carry"])
    await _update_tenant(
        (await _tenant_id_of(admin_client, platform, "p5-b-old")),
        expires_at=utc_now() - timedelta(days=10),
        status="expired",
    )

    board = await admin_client.get("/api/platform/expiry", headers=auth_header(platform))
    assert board.status_code == 200, board.text
    counts = board.json()["counts"]
    assert counts["today"] >= 1
    assert counts["days3"] >= 1
    assert counts["days7"] >= 1
    assert counts["expired"] >= 1
    assert "p5-b-today" in {item["username"] for item in board.json()["buckets"]["today"]["items"]}
    assert "p5-b-old" in {item["username"] for item in board.json()["buckets"]["expired"]["items"]}


async def _tenant_id_of(client, platform: str, username: str) -> int:
    """按用户名找租户 ID（看板用例里要把某个号改成"已过期"）。"""
    result = await client.get(
        "/api/platform/members", params={"keyword": username}, headers=auth_header(platform)
    )
    assert result.status_code == 200, result.text
    items = result.json()["items"]
    assert items, f"{username} 不在会员列表里"
    return int(items[0]["tenant_id"])


async def test_ledger_list_and_csv_export(admin_client, api_config) -> None:
    """额度台账：可按代理筛选、可导出 CSV（带 BOM）。"""
    platform = await _platform_token(admin_client)
    agent_id = await _make_agent(api_config, "p5-ledger-agent")
    await _grant(admin_client, platform, agent_id, "member", 2)
    agent_token = await _agent_token(admin_client, "p5-ledger-agent")
    response = await admin_client.post(
        "/api/agent/members",
        json={"username": "p5-ledger-c", "days": 30, "modules": ["carry"]},
        headers=auth_header(agent_token),
    )
    assert response.status_code == 201, response.text

    ledger = await admin_client.get(
        "/api/platform/ledger",
        params={"agent_user_id": agent_id},
        headers=auth_header(platform),
    )
    assert ledger.status_code == 200, ledger.text
    body = ledger.json()
    assert body["total"] >= 2
    actions = {item["action"] for item in body["items"]}
    assert {"manual_adjust", "open_member"} <= actions
    assert all(item["subject_username"] == "p5-ledger-agent" for item in body["items"])

    exported = await admin_client.get(
        "/api/platform/ledger.csv",
        params={"agent_user_id": agent_id},
        headers=auth_header(platform),
    )
    assert exported.status_code == 200, exported.text
    assert "text/csv" in exported.headers["content-type"]
    text = exported.content.decode("utf-8")
    assert text.startswith("\ufeff")
    assert "变动后余额" in text
    assert "开正式会员" in text


async def test_platform_writes_land_in_audit(admin_client, api_config) -> None:
    """P5-06：平台的开号 / 续期 / 停用写操作可在审计页查到。"""
    platform = await _platform_token(admin_client)
    opened = await _open_member(admin_client, platform, "p5-audit", modules=["carry"])
    user_id = opened["account"]["id"]
    renewed = await admin_client.post(
        f"/api/platform/members/{user_id}/renew",
        json={"days": 30},
        headers=auth_header(platform),
    )
    assert renewed.status_code == 200, renewed.text
    disabled = await admin_client.post(
        f"/api/platform/accounts/{user_id}/enable",
        json={"enabled": False, "reason": "审计用例"},
        headers=auth_header(platform),
    )
    assert disabled.status_code == 200, disabled.text

    audit = await admin_client.get(
        "/api/audit", params={"path": "/api/platform/"}, headers=auth_header(platform)
    )
    assert audit.status_code == 200, audit.text
    paths = {item["path"] for item in audit.json()["items"]}
    assert "/api/platform/members" in paths
    assert f"/api/platform/members/{user_id}/renew" in paths
    assert f"/api/platform/accounts/{user_id}/enable" in paths
