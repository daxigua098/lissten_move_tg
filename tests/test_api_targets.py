"""接收组接口测试：用途标记、权限预检与移出。"""

from __future__ import annotations

from conftest import ADMIN_API_TOKEN, auth_header, make_entity


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


def _fill(client) -> None:
    main_channel = make_entity(2001, "我的主频道", broadcast=True, username="my_main")
    lead_group = make_entity(2002, "线索收集群", participants_count=30)
    readonly_channel = make_entity(2003, "只读频道", broadcast=True, username="readonly_ch")
    client.dialogs = [main_channel, lead_group, readonly_channel]
    client.entities = {
        "my_main": main_channel,
        2001: main_channel,
        2002: lead_group,
        2003: readonly_channel,
        "readonly_ch": readonly_channel,
    }
    # 只读频道：替身报告"无发言权限"
    client.permissions = {2001: True, 2002: True, 2003: False}


async def test_add_targets_sets_role_and_checks_permission(
    chat_client,
    fake_account_client,
) -> None:
    _fill(fake_account_client)
    await chat_client.post("/api/sources/sync", headers=_headers())
    pool = (await chat_client.get("/api/targets/available", headers=_headers())).json()["items"]
    ids = {item["title"]: item["id"] for item in pool}

    response = await chat_client.post(
        "/api/targets",
        headers=_headers(),
        json={
            "chat_ids": [ids["我的主频道"], ids["只读频道"]],
            "role": "content",
            "tags": ["主推"],
        },
    )

    body = response.json()
    assert response.status_code == 201
    assert len(body["added"]) == 2
    by_title = {item["title"]: item for item in body["added"]}
    assert by_title["我的主频道"]["can_post"] is True
    assert by_title["只读频道"]["can_post"] is False
    assert by_title["我的主频道"]["target_role_label"] == "内容接收"
    assert by_title["我的主频道"]["tags"] == ["主推"]


async def test_target_role_lead_and_available_excludes_targets(
    chat_client,
    fake_account_client,
) -> None:
    _fill(fake_account_client)
    await chat_client.post("/api/sources/sync", headers=_headers())
    pool = (await chat_client.get("/api/targets/available", headers=_headers())).json()["items"]
    lead_id = next(item["id"] for item in pool if item["title"] == "线索收集群")

    added = await chat_client.post(
        "/api/targets",
        headers=_headers(),
        json={"chat_ids": [lead_id], "role": "lead"},
    )
    assert added.json()["added"][0]["target_role"] == "lead"
    assert added.json()["added"][0]["target_role_label"] == "线索接收"

    # 已成为接收组的群不再出现在可选列表
    available = (await chat_client.get("/api/targets/available", headers=_headers())).json()
    assert all(item["id"] != lead_id for item in available["items"])
    assert available["total"] == 2

    filtered = await chat_client.get("/api/targets?role=lead", headers=_headers())
    assert filtered.json()["total"] == 1


async def test_update_target_role_and_remove(chat_client, fake_account_client) -> None:
    _fill(fake_account_client)
    await chat_client.post("/api/sources/sync", headers=_headers())
    pool = (await chat_client.get("/api/targets/available", headers=_headers())).json()["items"]
    chat_id = next(item["id"] for item in pool if item["title"] == "线索收集群")
    await chat_client.post("/api/targets", headers=_headers(), json={"chat_ids": [chat_id]})

    switched = await chat_client.patch(
        f"/api/targets/{chat_id}",
        headers=_headers(),
        json={"role": "lead", "enabled": False, "note": "只收线索"},
    )
    body = switched.json()
    assert switched.status_code == 200
    assert body["target_role"] == "lead"
    assert body["target_enabled"] is False
    assert body["note"] == "只收线索"

    removed = await chat_client.delete(f"/api/targets/{chat_id}", headers=_headers())
    assert removed.status_code == 200
    assert (await chat_client.get("/api/targets", headers=_headers())).json()["total"] == 0

    missing = await chat_client.patch(
        f"/api/targets/{chat_id}",
        headers=_headers(),
        json={"role": "lead"},
    )
    assert missing.status_code == 404


async def test_target_inputs_resolve_links(chat_client, fake_account_client) -> None:
    _fill(fake_account_client)

    response = await chat_client.post(
        "/api/targets",
        headers=_headers(),
        json={"inputs": ["t.me/my_main"], "role": "content", "check_access": False},
    )

    body = response.json()
    assert response.status_code == 201
    assert body["added"][0]["tg_id"] == 2001
    # check_access=False 时不做预检
    assert body["added"][0]["can_post"] is None
