"""P3-06：代理工作台接口（开号、额度、下级树、续期、停用、隔离）。"""

from __future__ import annotations

from conftest import auth_header, login


async def _platform_token(client) -> str:
    response = await login(client)
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def _make_agent(config, username, *, parent_user_id=None, enabled=True):
    """直接建一个"已改过密码"的代理，省掉首次改密那一步。"""
    from app.db.session import session_scope
    from app.services import user_service

    async with session_scope() as session:
        user = await user_service.create_user(
            session,
            config,
            username=username,
            password="AgentPass123",
            account_type="agent",
            parent_user_id=parent_user_id,
            must_change_password=False,
        )
        user.enabled = enabled
        await session.commit()
        return user.id


async def _login_token(client, username: str, password: str) -> str:
    response = await login(client, username, password)
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def test_agent_endpoints_require_agent_or_platform(admin_client, api_config) -> None:
    """会员账号访问代理工作台一律 403。"""
    token = await _platform_token(admin_client)
    opened = await admin_client.post(
        "/api/agent/members",
        json={"username": "wb-member", "days": 30, "modules": ["carry"]},
        headers=auth_header(token),
    )
    assert opened.status_code == 201, opened.text
    password = opened.json()["initial_password"]
    assert opened.json()["tenant"]["quota_type"] == "none"  # 平台开号不占额度

    member_token = await _login_token(admin_client, "wb-member", password)
    changed = await admin_client.patch(
        "/api/auth/password",
        json={"current_password": password, "new_password": "MemberPass123"},
        headers=auth_header(member_token),
    )
    assert changed.status_code == 200, changed.text
    member_token = await _login_token(admin_client, "wb-member", "MemberPass123")

    for method, path in (("get", "/api/agent/quota"), ("get", "/api/agent/subordinates")):
        response = await getattr(admin_client, method)(path, headers=auth_header(member_token))
        assert response.status_code == 403, response.text


async def test_agent_can_open_member_until_quota_runs_out(admin_client, api_config) -> None:
    """代理开会员：额度够就 201，额度用完返回 409 QUOTA_INSUFFICIENT。"""
    platform = await _platform_token(admin_client)
    agent_id = await _make_agent(api_config, "wc-agent")

    granted = await admin_client.post(
        f"/api/agent/{agent_id}/adjust",
        json={"quota_type": "member", "delta": 1, "note": "试点发放"},
        headers=auth_header(platform),
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["quota"]["member"] == 1

    agent = await _login_token(admin_client, "wc-agent", "AgentPass123")
    first = await admin_client.post(
        "/api/agent/members",
        json={"username": "wc-customer", "days": 30, "modules": ["carry", "monitor"]},
        headers=auth_header(agent),
    )
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["account"]["account_type"] == "member"
    assert body["tenant"]["quota_type"] == "member"
    assert body["tenant"]["quota_held"] is True
    assert body["initial_password"] == "a123456"

    second = await admin_client.post(
        "/api/agent/members",
        json={"username": "wc-customer2", "days": 30, "modules": ["carry"]},
        headers=auth_header(agent),
    )
    assert second.status_code == 409
    assert second.json()["code"] == "QUOTA_INSUFFICIENT"

    overview = await admin_client.get("/api/agent/quota", headers=auth_header(agent))
    assert overview.status_code == 200
    assert overview.json()["quota"]["member"] == 0
    assert overview.json()["held"]["member"] == 1


async def test_agent_cannot_touch_grandchild_and_sees_no_business_data(
    admin_client, api_config
) -> None:
    """隔层只读：能看到整棵子树，但改隔层一律 403；代理看不到业务接口。"""
    platform = await _platform_token(admin_client)
    agent_id = await _make_agent(api_config, "wd-agent")
    await admin_client.post(
        f"/api/agent/{agent_id}/adjust",
        json={"quota_type": "member", "delta": 5, "note": "试点发放"},
        headers=auth_header(platform),
    )
    await admin_client.post(
        f"/api/agent/{agent_id}/adjust",
        json={"quota_type": "agent", "delta": 1, "note": "试点发放"},
        headers=auth_header(platform),
    )
    agent = await _login_token(admin_client, "wd-agent", "AgentPass123")

    child = await admin_client.post(
        "/api/agent/agents",
        json={"username": "wd-child", "allocate": {"member": 2}},
        headers=auth_header(agent),
    )
    assert child.status_code == 201, child.text
    assert child.json()["allocated"] == {"member": 2}
    assert child.json()["quota"]["member"] == 2

    child_password = child.json()["initial_password"]
    child_token = await _login_token(admin_client, "wd-child", child_password)
    # 新账号首次登录必须改密，改完再继续
    forced = await admin_client.get("/api/agent/quota", headers=auth_header(child_token))
    assert forced.status_code == 403
    assert forced.json()["code"] == "AUTH_PASSWORD_CHANGE_REQUIRED"
    await admin_client.patch(
        "/api/auth/password",
        json={"current_password": child_password, "new_password": "ChildPass123"},
        headers=auth_header(child_token),
    )
    child_token = await _login_token(admin_client, "wd-child", "ChildPass123")
    grandchild = await admin_client.post(
        "/api/agent/members",
        json={"username": "wd-grand", "days": 30, "modules": ["monitor"]},
        headers=auth_header(child_token),
    )
    assert grandchild.status_code == 201, grandchild.text
    grandchild_id = grandchild.json()["account"]["id"]

    tree = await admin_client.get("/api/agent/subordinates", headers=auth_header(agent))
    assert tree.status_code == 200
    rows = {item["username"]: item for item in tree.json()["items"]}
    assert rows["wd-child"]["is_direct"] is True
    assert rows["wd-grand"]["is_direct"] is False
    assert rows["wd-grand"]["depth"] == 2
    # 列表只给账号与到期信息，不带任何业务数据
    assert "modules" not in rows["wd-grand"]

    blocked = await admin_client.post(
        f"/api/agent/{grandchild_id}/renew",
        json={"days": 30},
        headers=auth_header(agent),
    )
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "AGENT_NOT_DIRECT_SUBORDINATE"

    for path in ("/api/leads", "/api/accounts", "/api/routes"):
        response = await admin_client.get(path, headers=auth_header(agent))
        assert response.status_code == 403, f"{path} -> {response.status_code}"


async def test_trial_is_fixed_one_day_via_api(admin_client, api_config) -> None:
    """试用固定 1 天：请求里没有天数可传，到期切点是当天 23:59:59。"""
    from datetime import datetime

    from app.core.expiry import expiry_for_days

    platform = await _platform_token(admin_client)
    agent_id = await _make_agent(api_config, "we-agent")
    await admin_client.post(
        f"/api/agent/{agent_id}/adjust",
        json={"quota_type": "trial", "delta": 2, "note": "试点发放"},
        headers=auth_header(platform),
    )
    agent = await _login_token(admin_client, "we-agent", "AgentPass123")

    opened = await admin_client.post(
        "/api/agent/trials",
        json={"username": "we-trial", "template_code": "trial_carry"},
        headers=auth_header(agent),
    )
    assert opened.status_code == 201, opened.text
    body = opened.json()
    assert body["modules"] == ["carry"]
    assert body["limits"] == {"max_routes": 1, "allow_export": False}
    assert datetime.fromisoformat(body["tenant"]["expires_at"]) == expiry_for_days(1)
    assert body["tenant"]["quota_type"] == "trial"

    overview = await admin_client.get("/api/agent/quota", headers=auth_header(agent))
    assert overview.json()["quota"]["trial"] == 1


async def test_renew_and_toggle_direct_subordinate(admin_client, api_config) -> None:
    """续期与停用只对直属下级生效；停用后会员登录被拒。"""
    platform = await _platform_token(admin_client)
    agent_id = await _make_agent(api_config, "wf-agent")
    await admin_client.post(
        f"/api/agent/{agent_id}/adjust",
        json={"quota_type": "member", "delta": 2, "note": "试点发放"},
        headers=auth_header(platform),
    )
    agent = await _login_token(admin_client, "wf-agent", "AgentPass123")

    opened = await admin_client.post(
        "/api/agent/members",
        json={"username": "wf-customer", "days": 1, "modules": ["monitor"]},
        headers=auth_header(agent),
    )
    assert opened.status_code == 201, opened.text
    customer_id = opened.json()["account"]["id"]
    password = opened.json()["initial_password"]
    # 开号后额度已释放干净地被占用，续期（未到期）不该再扣
    renewed = await admin_client.post(
        f"/api/agent/{customer_id}/renew",
        json={"days": 30},
        headers=auth_header(agent),
    )
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["tenant"]["quota_held"] is True

    overview = await admin_client.get("/api/agent/quota", headers=auth_header(agent))
    assert overview.json()["quota"]["member"] == 1  # 只扣了开号那 1 个

    disabled = await admin_client.post(
        f"/api/agent/{customer_id}/enable",
        json={"enabled": False},
        headers=auth_header(agent),
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["account"]["enabled"] is False
    assert disabled.json()["account"]["tenant_status"] == "suspended"

    blocked = await login(admin_client, "wf-customer", password)
    assert blocked.status_code == 401

    enabled = await admin_client.post(
        f"/api/agent/{customer_id}/enable",
        json={"enabled": True},
        headers=auth_header(agent),
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["account"]["enabled"] is True
    assert enabled.json()["account"]["tenant_status"] == "active"


async def test_ledger_and_templates_and_stats(admin_client, api_config) -> None:
    """工作台数据来源：流水、模板、统计都能取到。"""
    platform = await _platform_token(admin_client)
    agent_id = await _make_agent(api_config, "wg-agent")
    await admin_client.post(
        f"/api/agent/{agent_id}/adjust",
        json={"quota_type": "member", "delta": 3, "note": "试点发放"},
        headers=auth_header(platform),
    )
    agent = await _login_token(admin_client, "wg-agent", "AgentPass123")
    await admin_client.post(
        "/api/agent/members",
        json={"username": "wg-customer", "days": 30, "modules": ["carry"]},
        headers=auth_header(agent),
    )

    ledger = await admin_client.get("/api/agent/ledger?scope=subtree", headers=auth_header(agent))
    assert ledger.status_code == 200
    actions = [item["action"] for item in ledger.json()["items"]]
    assert "manual_adjust" in actions
    assert "open_member" in actions

    templates = await admin_client.get("/api/agent/templates", headers=auth_header(agent))
    assert templates.status_code == 200
    codes = [item["code"] for item in templates.json()["items"]]
    assert "trial_carry" in codes and "full" in codes

    stats = await admin_client.get("/api/agent/stats", headers=auth_header(agent))
    assert stats.status_code == 200
    assert stats.json()["opened_this_month"] == 1
    assert stats.json()["active_members"] == 1

    expiring = await admin_client.get("/api/agent/expiring?days=90", headers=auth_header(agent))
    assert expiring.status_code == 200
    assert [item["username"] for item in expiring.json()["items"]] == ["wg-customer"]


async def test_adjust_is_platform_only(admin_client, api_config) -> None:
    """手工调账只有平台能做；代理发起一律 403。"""
    platform = await _platform_token(admin_client)
    agent_id = await _make_agent(api_config, "wh-agent")
    await admin_client.post(
        f"/api/agent/{agent_id}/adjust",
        json={"quota_type": "member", "delta": 1, "note": "试点发放"},
        headers=auth_header(platform),
    )
    agent = await _login_token(admin_client, "wh-agent", "AgentPass123")
    sub = await _make_agent(api_config, "wh-sub", parent_user_id=agent_id)

    response = await admin_client.post(
        f"/api/agent/{sub}/adjust",
        json={"quota_type": "member", "delta": 1, "note": "自己给自己发"},
        headers=auth_header(agent),
    )
    assert response.status_code == 403

    # 代理给自己划拨也不行（额度只能来自上级）
    response = await admin_client.post(
        "/api/agent/allocate",
        json={"target_user_id": agent_id, "quota_type": "member", "count": 1},
        headers=auth_header(agent),
    )
    assert response.status_code == 403

    # 调账必须写备注
    response = await admin_client.post(
        f"/api/agent/{agent_id}/adjust",
        json={"quota_type": "member", "delta": 1, "note": ""},
        headers=auth_header(platform),
    )
    assert response.status_code == 422


async def test_agent_can_reclaim_from_direct_subordinate(admin_client, api_config) -> None:
    """回收走后端：把划给下级的未使用额度拿回来。"""
    platform = await _platform_token(admin_client)
    agent_id = await _make_agent(api_config, "wi-agent")
    await admin_client.post(
        f"/api/agent/{agent_id}/adjust",
        json={"quota_type": "member", "delta": 5, "note": "试点发放"},
        headers=auth_header(platform),
    )
    await admin_client.post(
        f"/api/agent/{agent_id}/adjust",
        json={"quota_type": "agent", "delta": 1, "note": "试点发放"},
        headers=auth_header(platform),
    )
    agent = await _login_token(admin_client, "wi-agent", "AgentPass123")
    child = await admin_client.post(
        "/api/agent/agents",
        json={"username": "wi-child", "allocate": {"member": 3}},
        headers=auth_header(agent),
    )
    assert child.status_code == 201, child.text
    child_id = child.json()["account"]["id"]

    reclaimed = await admin_client.post(
        "/api/agent/reclaim",
        json={"target_user_id": child_id, "quota_type": "member", "count": 2},
        headers=auth_header(agent),
    )
    assert reclaimed.status_code == 200, reclaimed.text
    assert reclaimed.json()["count"] == 2

    overview = await admin_client.get("/api/agent/quota", headers=auth_header(agent))
    assert overview.json()["quota"]["member"] == 4
