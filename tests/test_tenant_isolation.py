"""P1-07：跨租户越权用例（会员只看得到、只改得动自己租户的业务数据）。

P1-05 把「请求级租户作用域」落在两条链路上：

- **写入**：``TenantOwnedMixin.tenant_id`` 的默认值取当前作用域，
  会员在「账号与机器人」「线路配置」「词库」里建的东西自动落到自己的租户；
- **读取**：``app.db.session`` 注册的 ORM 事件给业务表补 ``tenant_id`` 条件，
  列表、详情、导出、运行总览都拿不到别人的行。

这一组用例从接口层钉住越权边界：会员 A 建的 TG 账号 / 线路 / 词表 / 线索，
会员 B 既看不到（列表与导出不串）、也改不动（按 ID 直接操作 404）。
平台账号的作用域是自营租户，所以它同样看不到会员的账号。
"""

from __future__ import annotations

from typing import Any

from conftest import auth_header, login

from app.db.session import session_scope

AGENT_PASSWORD = "AgentPass123"
MEMBER_PASSWORD = "MemberPass123"

ACCOUNT_PAYLOAD = {
    "name": "主号",
    "phone": "+8613800009001",
    "api_id": 123456,
    "api_hash": "abcdef0123456789abcdef0123456789",
}


async def _platform_token(client) -> str:
    response = await login(client)
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def _first_login(client, username: str, initial: str, final: str) -> str:
    """首次登录 → 改密 → 重新登录，返回可用令牌。"""
    token = (await login(client, username, initial)).json()["token"]
    changed = await client.patch(
        "/api/auth/password",
        json={"current_password": initial, "new_password": final},
        headers=auth_header(token),
    )
    assert changed.status_code == 200, changed.text
    response = await login(client, username, final)
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def _open_agent(client, platform: str, *, username: str) -> int:
    response = await client.post(
        "/api/agent/agents",
        json={"username": username, "password": AGENT_PASSWORD},
        headers=auth_header(platform),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["account"]["id"])


async def _grant(client, platform: str, agent_id: int, quota_type: str, delta: int) -> None:
    response = await client.post(
        f"/api/agent/{agent_id}/adjust",
        json={"quota_type": quota_type, "delta": delta, "note": "隔离用例发放"},
        headers=auth_header(platform),
    )
    assert response.status_code == 200, response.text


async def _open_member(client, agent_token: str, *, username: str) -> dict[str, Any]:
    response = await client.post(
        "/api/agent/members",
        json={"username": username, "days": 30, "modules": ["carry", "monitor"]},
        headers=auth_header(agent_token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _two_members(client) -> dict[str, Any]:
    """一个代理开两个会员，返回两边的令牌与租户 ID。"""
    platform = await _platform_token(client)
    agent_id = await _open_agent(client, platform, username="iso-agent")
    await _grant(client, platform, agent_id, "member", 2)
    agent = await _first_login(client, "iso-agent", AGENT_PASSWORD, AGENT_PASSWORD)

    owner = await _open_member(client, agent, username="iso-owner")
    other = await _open_member(client, agent, username="iso-other")
    owner_tenant = int(owner["tenant"]["id"])
    other_tenant = int(other["tenant"]["id"])
    assert owner_tenant != other_tenant

    return {
        "tenant_a": owner_tenant,
        "tenant_b": other_tenant,
        "member_a": await _first_login(
            client, "iso-owner", owner["initial_password"], MEMBER_PASSWORD
        ),
        "member_b": await _first_login(
            client, "iso-other", other["initial_password"], MEMBER_PASSWORD
        ),
    }


async def _seed_chat(tenant_id: int, tg_id: int, *, title: str) -> int:
    """在指定租户下铺一个池内群组，返回 chat_id。"""
    from app.core.telegram_client import ChatProfile
    from app.services import chat_service

    async with session_scope() as session:
        chat = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=tg_id,
                chat_type="channel",
                title=title,
                username=f"src{tg_id}",
                is_private=False,
            ),
            tenant_id=tenant_id,
        )
        return int(chat.id)


async def _seed_route(tenant_id: int, *, name: str, enabled: bool = True) -> int:
    """在指定租户下直接铺一条线路（不走接口，专供越权用例造数据）。"""
    from app.db.models import Route

    source_chat_id = await _seed_chat(tenant_id, tg_id=7_700_000 + tenant_id, title=name)
    async with session_scope() as session:
        route = Route(
            name=name,
            source_chat_id=source_chat_id,
            business_type="A",
            tenant_id=tenant_id,
            enabled=enabled,
        )
        session.add(route)
        await session.commit()
        return int(route.id)


async def _mark_chat(
    tenant_id: int,
    chat_id: int,
    *,
    source: bool = False,
    target: bool = False,
) -> None:
    """把池内群组标记成监听源 / 接收组（建线的前置校验要求这两个标记）。"""
    from app.db.tenant_context import tenant_scope
    from app.services import chat_service

    async with session_scope() as session:
        with tenant_scope(tenant_id):
            chat = await chat_service.get_chat(session, chat_id)
            assert chat is not None
            if source:
                await chat_service.set_source(session, chat, enabled=True)
            if target:
                await chat_service.set_target(session, chat, role="content", enabled=True)


async def _seed_lead(tenant_id: int, *, text: str, keyword: str) -> int:
    """在指定租户下落一条线索。"""
    from app.db.models import Lead

    async with session_scope() as session:
        lead = Lead(
            tenant_id=tenant_id,
            message_id=880_001,
            sender_tg_id=555_001,
            sender_name="张三",
            keyword=keyword,
            matched_mode="contains",
            score=1.0,
            text=text,
            source_title="隔离源",
        )
        session.add(lead)
        await session.commit()
        return int(lead.id)


async def test_member_tg_accounts_stay_in_own_tenant(admin_client) -> None:
    """会员建的 TG 账号归自己租户：列表不串、按 ID 也改不动。"""
    from sqlalchemy import select

    from app.db.models import TgAccount

    both = await _two_members(admin_client)
    member_a, member_b = both["member_a"], both["member_b"]

    created = await admin_client.post(
        "/api/accounts", json=ACCOUNT_PAYLOAD, headers=auth_header(member_a)
    )
    assert created.status_code == 201, created.text
    account_id = int(created.json()["id"])

    async with session_scope() as session:
        owner = await session.scalar(select(TgAccount.tenant_id).where(TgAccount.id == account_id))
    assert owner == both["tenant_a"]

    mine = await admin_client.get("/api/accounts", headers=auth_header(member_a))
    assert [item["id"] for item in mine.json()["items"]] == [account_id]
    assert mine.json()["total"] == 1

    seen = await admin_client.get("/api/accounts", headers=auth_header(member_b))
    assert seen.json()["items"] == []
    assert seen.json()["total"] == 0

    patched = await admin_client.patch(
        f"/api/accounts/{account_id}", json={"name": "被越权改名"}, headers=auth_header(member_b)
    )
    assert patched.status_code == 404, patched.text
    deleted = await admin_client.delete(
        f"/api/accounts/{account_id}", headers=auth_header(member_b)
    )
    assert deleted.status_code == 404, deleted.text

    # 账号名只在租户内唯一：B 也叫「主号」照样建得出来
    same_name = await admin_client.post(
        "/api/accounts", json=ACCOUNT_PAYLOAD, headers=auth_header(member_b)
    )
    assert same_name.status_code == 201, same_name.text
    assert int(same_name.json()["id"]) != account_id


async def test_platform_accounts_are_self_tenant_only(admin_client) -> None:
    """平台建的账号落在自营租户：会员看不到，平台也看不到会员的。"""
    from sqlalchemy import select

    from app.db.models import SELF_TENANT_ID, TgAccount

    platform = await _platform_token(admin_client)
    created = await admin_client.post(
        "/api/accounts", json=ACCOUNT_PAYLOAD, headers=auth_header(platform)
    )
    assert created.status_code == 201, created.text
    platform_account_id = int(created.json()["id"])

    async with session_scope() as session:
        owner = await session.scalar(
            select(TgAccount.tenant_id).where(TgAccount.id == platform_account_id)
        )
    assert owner == SELF_TENANT_ID

    both = await _two_members(admin_client)
    member_a = both["member_a"]

    seen = await admin_client.get("/api/accounts", headers=auth_header(member_a))
    assert seen.json()["total"] == 0
    patched = await admin_client.patch(
        f"/api/accounts/{platform_account_id}",
        json={"name": "会员改自营账号"},
        headers=auth_header(member_a),
    )
    assert patched.status_code == 404, patched.text

    mine = await admin_client.post(
        "/api/accounts", json=ACCOUNT_PAYLOAD, headers=auth_header(member_a)
    )
    assert mine.status_code == 201, mine.text
    member_account_id = int(mine.json()["id"])

    # 反向：平台侧的作用域是自营租户，看不到会员的账号
    platform_list = await admin_client.get("/api/accounts", headers=auth_header(platform))
    ids = [item["id"] for item in platform_list.json()["items"]]
    assert platform_account_id in ids
    assert member_account_id not in ids


async def test_routes_are_scoped_to_owner_tenant(admin_client) -> None:
    """线路归属与读写都按租户收口：别人的线路既看不到也改不动。"""
    from sqlalchemy import select

    from app.db.models import Route

    both = await _two_members(admin_client)
    member_a, member_b = both["member_a"], both["member_b"]
    tenant_a = both["tenant_a"]

    source_id = await _seed_chat(tenant_a, 8_100_001, title="A 的源")
    target_id = await _seed_chat(tenant_a, 8_100_002, title="A 的接收组")
    await _mark_chat(tenant_a, source_id, source=True)
    await _mark_chat(tenant_a, target_id, target=True)

    created = await admin_client.post(
        "/api/routes",
        json={
            "name": "A 的线路",
            "source_chat_id": source_id,
            "business_type": "A",
            "a_config": {"ad_policy": "none"},
            "target_chat_ids": [target_id],
        },
        headers=auth_header(member_a),
    )
    assert created.status_code == 201, created.text
    route_id = int(created.json()["id"])

    async with session_scope() as session:
        owner = await session.scalar(select(Route.tenant_id).where(Route.id == route_id))
    assert owner == tenant_a

    listed_a = await admin_client.get("/api/routes", headers=auth_header(member_a))
    assert [item["id"] for item in listed_a.json()["items"]] == [route_id]
    assert listed_a.json()["total"] == 1

    listed_b = await admin_client.get("/api/routes", headers=auth_header(member_b))
    assert listed_b.json()["items"] == []
    assert listed_b.json()["total"] == 0

    detail = await admin_client.get(f"/api/routes/{route_id}", headers=auth_header(member_b))
    assert detail.status_code == 404, detail.text
    patched = await admin_client.patch(
        f"/api/routes/{route_id}", json={"name": "被越权改名"}, headers=auth_header(member_b)
    )
    assert patched.status_code == 404, patched.text
    deleted = await admin_client.delete(f"/api/routes/{route_id}", headers=auth_header(member_b))
    assert deleted.status_code == 404, deleted.text

    # B 也不能拿 A 的群组当源建线（源在作用域外，等于不存在）
    crossed = await admin_client.post(
        "/api/routes",
        json={
            "name": "越权线路",
            "source_chat_id": source_id,
            "business_type": "A",
            "a_config": {"ad_policy": "none"},
            "target_chat_ids": [target_id],
        },
        headers=auth_header(member_b),
    )
    assert crossed.status_code in {400, 404}, crossed.text

    # A 的线路还在，没被越权操作带走
    again = await admin_client.get("/api/routes", headers=auth_header(member_a))
    assert [item["id"] for item in again.json()["items"]] == [route_id]


async def test_keyword_groups_are_scoped_to_owner_tenant(admin_client) -> None:
    """词库按租户隔离：会员 B 看不到也改不动会员 A 的词组。"""
    both = await _two_members(admin_client)
    member_a, member_b = both["member_a"], both["member_b"]

    created = await admin_client.post(
        "/api/keyword-groups",
        json={"name": "A 的词表"},
        headers=auth_header(member_a),
    )
    assert created.status_code == 201, created.text
    group_id = int(created.json()["id"])

    mine = await admin_client.get("/api/keyword-groups", headers=auth_header(member_a))
    assert [item["id"] for item in mine.json()["items"]] == [group_id]

    seen = await admin_client.get("/api/keyword-groups", headers=auth_header(member_b))
    assert seen.json()["items"] == []

    patched = await admin_client.patch(
        f"/api/keyword-groups/{group_id}",
        json={"name": "被越权改名"},
        headers=auth_header(member_b),
    )
    assert patched.status_code == 404, patched.text
    deleted = await admin_client.delete(
        f"/api/keyword-groups/{group_id}", headers=auth_header(member_b)
    )
    assert deleted.status_code == 404, deleted.text


async def test_leads_and_export_are_scoped_to_owner_tenant(admin_client) -> None:
    """线索是业务数据：会员 B 的列表、统计与 CSV 导出里都没有 A 的线索。"""
    both = await _two_members(admin_client)
    member_a, member_b = both["member_a"], both["member_b"]
    await _seed_lead(both["tenant_a"], text="A 的线索内容", keyword="贷款")
    await _seed_lead(both["tenant_b"], text="B 的线索内容", keyword="贷款")

    listed_a = await admin_client.get("/api/leads", headers=auth_header(member_a))
    assert listed_a.json()["total"] == 1
    assert [item["text"] for item in listed_a.json()["items"]] == ["A 的线索内容"]

    listed_b = await admin_client.get("/api/leads", headers=auth_header(member_b))
    assert listed_b.json()["total"] == 1
    assert [item["text"] for item in listed_b.json()["items"]] == ["B 的线索内容"]

    exported = await admin_client.get("/api/leads/export.csv", headers=auth_header(member_b))
    assert exported.status_code == 200, exported.text
    assert "B 的线索内容" in exported.text
    assert "A 的线索内容" not in exported.text


async def test_start_all_only_touches_own_tenant(admin_client, monkeypatch) -> None:
    """会员的「一键启动」只动自己租户的线路，运行总览也不显示别人的线路号。"""
    from sqlalchemy import select

    from app.db.models import Route, Tenant
    from app.services import runtime_service

    async def _no_process(config, **_kwargs):
        return {"started": False, "reason": "test-stub"}

    monkeypatch.setattr(runtime_service, "start_runtime_process", _no_process)

    both = await _two_members(admin_client)
    member_b = both["member_b"]
    route_a = await _seed_route(both["tenant_a"], name="A 的线路", enabled=False)
    route_b = await _seed_route(both["tenant_b"], name="B 的线路", enabled=False)

    started = await admin_client.post("/api/runtime/start-all", headers=auth_header(member_b))
    assert started.status_code == 200, started.text
    assert started.json()["enabled_routes"] == 1

    async with session_scope() as session:
        rows = await session.scalars(select(Route).where(Route.id.in_([route_a, route_b])))
        flags = {row.id: row.enabled for row in rows}
    assert flags[route_b] is True
    assert flags[route_a] is False

    status_b = await admin_client.get("/api/runtime/status", headers=auth_header(member_b))
    assert status_b.status_code == 200, status_b.text
    body_b = status_b.json()
    assert body_b["tenant"]["runtime_enabled"] is True
    assert route_a not in body_b["pending_route_ids"]

    # 一键启动没有顺手打开 A 的租户开关
    async with session_scope() as session:
        tenant_a_row = await session.get(Tenant, both["tenant_a"])
    assert tenant_a_row.runtime_enabled is False

    # B 的线路列表里只有自己的那条；A 的线路按 ID 取也是 404
    listed_b = await admin_client.get("/api/routes", headers=auth_header(member_b))
    assert [item["id"] for item in listed_b.json()["items"]] == [route_b]
    detail_a = await admin_client.get(f"/api/routes/{route_a}", headers=auth_header(member_b))
    assert detail_a.status_code == 404, detail_a.text


async def test_tenant_scope_has_explicit_escape_hatch(admin_client) -> None:
    """作用域之外留了显式越过的口子：``include_all_tenants=True``。

    非请求路径（运行时、脚本、巡检）本来就不设作用域，这一条钉住的是"请求内
    确实需要跨租户看一眼"的用法——必须写明白，不能靠漏掉过滤器碰运气。
    """
    from sqlalchemy import select

    from app.db.models import Route
    from app.db.tenant_context import tenant_scope

    both = await _two_members(admin_client)
    route_a = await _seed_route(both["tenant_a"], name="A 的线路")
    route_b = await _seed_route(both["tenant_b"], name="B 的线路")

    async with session_scope() as session:
        with tenant_scope(both["tenant_a"]):
            scoped = list(await session.scalars(select(Route.id)))
            crossed = list(
                await session.scalars(select(Route.id).execution_options(include_all_tenants=True))
            )
        unscoped = list(await session.scalars(select(Route.id)))

    assert scoped == [route_a]
    assert set(crossed) >= {route_a, route_b}
    assert set(unscoped) >= {route_a, route_b}
