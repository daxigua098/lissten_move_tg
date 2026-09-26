"""线路接口测试：矩阵建线、配置校验、目标与水位线。"""

from __future__ import annotations

from conftest import ADMIN_API_TOKEN, auth_header, make_entity


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


async def _prepare(client, fake) -> dict[str, int]:
    """同步群组池，并把 1 个源、2 个接收组建好。"""
    source = make_entity(3001, "素材源频道", broadcast=True, username="src_ch")
    main = make_entity(3002, "我的主频道", broadcast=True, username="main_ch")
    lead = make_entity(3003, "线索收集群")
    fake.dialogs = [source, main, lead]
    fake.entities = {"src_ch": source, "main_ch": main, 3003: lead}
    fake.permissions = {3001: True, 3002: True, 3003: True}

    await client.post("/api/sources/sync", headers=_headers())

    pool = (await client.get("/api/sources/available", headers=_headers())).json()["items"]
    source_id = next(item["id"] for item in pool if item["title"] == "素材源频道")
    await client.post("/api/sources", headers=_headers(), json={"chat_ids": [source_id]})

    target_pool = (await client.get("/api/targets/available", headers=_headers())).json()["items"]
    main_id = next(item["id"] for item in target_pool if item["title"] == "我的主频道")
    lead_id = next(item["id"] for item in target_pool if item["title"] == "线索收集群")
    await client.post(
        "/api/targets",
        headers=_headers(),
        json={"chat_ids": [main_id], "role": "content"},
    )
    await client.post(
        "/api/targets",
        headers=_headers(),
        json={"chat_ids": [lead_id], "role": "lead"},
    )
    return {"source": source_id, "main": main_id, "lead": lead_id}


async def test_matrix_create_skips_existing(chat_client, fake_account_client) -> None:
    ids = await _prepare(chat_client, fake_account_client)

    first = await chat_client.post(
        "/api/routes/matrix",
        headers=_headers(),
        json={
            "source_chat_ids": [ids["source"]],
            "target_chat_ids": [ids["main"], ids["lead"]],
            "business_type": "A",
            "a_config": {"ad_policy": "none"},
        },
    )
    body = first.json()
    assert first.status_code == 201
    assert body["created"] == 2
    assert body["skipped"] == []

    again = await chat_client.post(
        "/api/routes/matrix",
        headers=_headers(),
        json={
            "source_chat_ids": [ids["source"]],
            "target_chat_ids": [ids["main"], ids["lead"]],
            "business_type": "A",
            "a_config": {"ad_policy": "none"},
        },
    )
    assert again.json()["created"] == 0
    assert len(again.json()["skipped"]) == 2

    listed = await chat_client.get("/api/routes", headers=_headers())
    items = listed.json()["items"]
    assert listed.json()["total"] == 2
    assert {item["business_type"] for item in items} == {"A"}
    names = {item["name"] for item in items}
    assert "素材源频道 → 我的主频道" in names
    assert all(item["source"]["title"] == "素材源频道" for item in items)
    assert all(item["targets"][0]["last_delivered_message_id"] == 0 for item in items)


async def test_route_requires_configured_source_and_target(
    chat_client, fake_account_client
) -> None:
    ids = await _prepare(chat_client, fake_account_client)
    pool = (await chat_client.get("/api/targets/available", headers=_headers())).json()["items"]
    # 把源频道也当成目标（它是源不是目标）
    not_target = ids["source"]

    response = await chat_client.post(
        "/api/routes",
        headers=_headers(),
        json={
            "name": "错误线路",
            "source_chat_id": not_target,
            "business_type": "A",
            "target_chat_ids": [not_target],
        },
    )
    assert response.status_code == 400
    assert "接收组" in response.json()["detail"]
    assert pool  # 使用变量，避免未使用告警


async def test_a_line_ad_policy_requires_asset(chat_client, fake_account_client) -> None:
    ids = await _prepare(chat_client, fake_account_client)

    response = await chat_client.post(
        "/api/routes",
        headers=_headers(),
        json={
            "name": "缺素材的线路",
            "source_chat_id": ids["source"],
            "business_type": "A",
            "target_chat_ids": [ids["main"]],
            "a_config": {"ad_policy": "nth", "ad_nth": 3},
        },
    )

    assert response.status_code == 400
    assert "广告素材" in response.json()["detail"]

    asset = await chat_client.post(
        "/api/ad-assets",
        headers=_headers(),
        json={"name": "渠道A文案", "text": "每日更新"},
    )
    asset_id = asset.json()["id"]

    ok = await chat_client.post(
        "/api/routes",
        headers=_headers(),
        json={
            "name": "带素材的线路",
            "source_chat_id": ids["source"],
            "business_type": "A",
            "target_chat_ids": [ids["main"]],
            "a_config": {"ad_policy": "nth", "ad_nth": 3, "ad_asset_id": asset_id},
        },
    )
    assert ok.status_code == 201
    assert ok.json()["a_config"]["ad_asset_id"] == asset_id


async def test_b_line_keyword_mode_requires_group(chat_client, fake_account_client) -> None:
    ids = await _prepare(chat_client, fake_account_client)

    missing = await chat_client.post(
        "/api/routes",
        headers=_headers(),
        json={
            "name": "关键词监听",
            "source_chat_id": ids["source"],
            "business_type": "B",
            "target_chat_ids": [ids["lead"]],
            "b_config": {"listen_mode": "keyword"},
        },
    )
    assert missing.status_code == 400
    assert "关键词组" in missing.json()["detail"]

    ok = await chat_client.post(
        "/api/routes",
        headers=_headers(),
        json={
            "name": "关键词监听",
            "source_chat_id": ids["source"],
            "business_type": "B",
            "target_chat_ids": [ids["lead"]],
            "b_config": {"listen_mode": "keyword", "keyword_group_ids": [1]},
        },
    )
    assert ok.status_code == 201
    assert ok.json()["b_config"]["listen_mode"] == "keyword"
    assert ok.json()["targets"][0]["target_role"] == "lead"


async def test_update_route_and_targets(chat_client, fake_account_client) -> None:
    ids = await _prepare(chat_client, fake_account_client)
    created = await chat_client.post(
        "/api/routes",
        headers=_headers(),
        json={
            "name": "线路一",
            "source_chat_id": ids["source"],
            "business_type": "A",
            "target_chat_ids": [ids["main"]],
            "a_config": {"ad_policy": "none"},
        },
    )
    route_id = created.json()["id"]

    updated = await chat_client.patch(
        f"/api/routes/{route_id}",
        headers=_headers(),
        json={
            "name": "线路一（改名）",
            "enabled": False,
            "delay_seconds": 2.5,
            "a_config": {"ad_policy": "every", "ad_asset_id": None, "content_types": ["video"]},
        },
    )
    assert updated.status_code == 400  # every 模式必须有素材

    toggled = await chat_client.patch(
        f"/api/routes/{route_id}",
        headers=_headers(),
        json={"name": "线路一（改名）", "enabled": False, "delay_seconds": 2.5},
    )
    assert toggled.status_code == 200
    assert toggled.json()["name"] == "线路一（改名）"
    assert toggled.json()["enabled"] is False
    assert toggled.json()["delay_seconds"] == 2.5

    added = await chat_client.post(
        f"/api/routes/{route_id}/targets",
        headers=_headers(),
        json={"chat_ids": [ids["lead"]]},
    )
    assert added.status_code == 201
    assert added.json()["added"] == [ids["lead"]]
    assert added.json()["skipped"] == []

    duplicate = await chat_client.post(
        f"/api/routes/{route_id}/targets",
        headers=_headers(),
        json={"chat_ids": [ids["lead"]]},
    )
    assert duplicate.json()["added"] == []
    assert duplicate.json()["skipped"] == [ids["lead"]]

    disabled = await chat_client.patch(
        f"/api/routes/{route_id}/targets/{ids['lead']}",
        headers=_headers(),
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    removed = await chat_client.delete(
        f"/api/routes/{route_id}/targets/{ids['lead']}",
        headers=_headers(),
    )
    assert removed.status_code == 200
    detail = await chat_client.get(f"/api/routes/{route_id}", headers=_headers())
    assert len(detail.json()["targets"]) == 1


async def test_reset_progress_requires_confirm(chat_client, fake_account_client) -> None:
    ids = await _prepare(chat_client, fake_account_client)
    created = await chat_client.post(
        "/api/routes",
        headers=_headers(),
        json={
            "name": "线路",
            "source_chat_id": ids["source"],
            "business_type": "A",
            "target_chat_ids": [ids["main"]],
            "a_config": {"ad_policy": "none"},
        },
    )
    route_id = created.json()["id"]

    wrong = await chat_client.post(
        f"/api/routes/{route_id}/targets/{ids['main']}/reset",
        headers=_headers(),
        json={"confirm": "yes"},
    )
    assert wrong.status_code == 400
    assert "RESET" in wrong.json()["detail"]

    ok = await chat_client.post(
        f"/api/routes/{route_id}/targets/{ids['main']}/reset",
        headers=_headers(),
        json={"confirm": "reset"},
    )
    assert ok.status_code == 200
    assert ok.json()["last_delivered_message_id"] == 0
    assert ok.json()["reset"] is True

    progress = await chat_client.get(f"/api/routes/{route_id}/progress", headers=_headers())
    assert progress.json()["items"][0]["backfill_status"] == "idle"

    deleted = await chat_client.delete(f"/api/routes/{route_id}", headers=_headers())
    assert deleted.status_code == 200
    assert (await chat_client.get("/api/routes", headers=_headers())).json()["total"] == 0


async def test_create_route_with_blank_name_returns_readable_error(
    chat_client,
    fake_account_client,
) -> None:
    """线路名留空时报错要能看懂，不能直接甩 FastAPI 的英文原文。"""
    ids = await _prepare(chat_client, fake_account_client)

    response = await chat_client.post(
        "/api/routes",
        headers=_headers(),
        json={
            "name": "",
            "source_chat_id": ids["source"],
            "business_type": "A",
            "target_chat_ids": [ids["main"]],
        },
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["detail"] == "参数校验失败：名称不能为空"
    # 原始定位仍保留在 issues 里，便于排查
    assert body["issues"][0]["location"] == "body.name"


async def test_route_sender_mode_bot_needs_a_bot(bot_client, api_config) -> None:
    """选择「用机器人发送」时必须指定机器人；指定后能保存并回显。"""
    from test_api_bots import VALID_TOKEN

    from app.core.telegram_client import ChatProfile
    from app.db.session import session_scope
    from app.services import chat_service

    async with session_scope() as session:
        source = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=5101,
                chat_type="supergroup",
                title="发言方式测试源",
                username=None,
                is_private=True,
            ),
        )
        await chat_service.set_source(session, source)
        target = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=5102,
                chat_type="supergroup",
                title="发言方式测试目标",
                username=None,
                is_private=True,
            ),
        )
        await chat_service.set_target(session, target, role="lead")
        source_id, target_id = source.id, target.id

    base = {
        "name": "机器人发送线路",
        "source_chat_id": source_id,
        "business_type": "B",
        "target_chat_ids": [target_id],
        "b_config": {"listen_mode": "all"},
        "sender_mode": "bot",
    }
    bad = await bot_client.post("/api/routes", headers=_headers(), json=base)
    assert bad.status_code == 400
    assert "机器人" in bad.json()["detail"]

    bot = await bot_client.post(
        "/api/bots",
        headers=_headers(),
        json={"name": "发送机器人", "token": VALID_TOKEN},
    )
    assert bot.status_code == 201

    ok = await bot_client.post(
        "/api/routes",
        headers=_headers(),
        json={**base, "notify_bot_id": bot.json()["id"]},
    )
    assert ok.status_code == 201
    assert ok.json()["sender_mode"] == "bot"
    assert ok.json()["notify_bot_id"] == bot.json()["id"]

    # A 线（搬运帖子）不允许用机器人发送：机器人读不到源群帖子
    carry = await bot_client.post(
        "/api/routes",
        headers=_headers(),
        json={
            **base,
            "name": "搬运线路",
            "business_type": "A",
            "a_config": {"ad_policy": "none"},
            "notify_bot_id": bot.json()["id"],
        },
    )
    assert carry.status_code == 400
    assert "机器人无法转发帖子" in carry.json()["detail"]


async def test_target_switch_disables_delivery_in_all_routes(
    chat_client,
    fake_account_client,
) -> None:
    """接收组页的开关是总开关：关掉后所有线路上的该目标都要停。"""
    ids = await _prepare(chat_client, fake_account_client)
    created = await chat_client.post(
        "/api/routes",
        headers=_headers(),
        json={
            "name": "源 → 主频道",
            "source_chat_id": ids["source"],
            "business_type": "A",
            "target_chat_ids": [ids["main"]],
            "a_config": {"ad_policy": "none"},
        },
    )
    route_id = created.json()["id"]
    detail = (await chat_client.get(f"/api/routes/{route_id}", headers=_headers())).json()
    assert detail["targets"][0]["enabled"] is True

    patched = await chat_client.patch(
        f"/api/targets/{ids['main']}",
        headers=_headers(),
        json={"enabled": False},
    )

    assert patched.status_code == 200
    assert patched.json()["target_enabled"] is False
    detail = (await chat_client.get(f"/api/routes/{route_id}", headers=_headers())).json()
    assert detail["targets"][0]["enabled"] is False


async def test_route_with_multiple_sources_keeps_one_route_per_source(
    chat_client,
    fake_account_client,
) -> None:
    """多选监听源：一个源一条线路（保证水位线准确），共用 bundle。"""
    source_a = make_entity(3001, "素材源频道", broadcast=True, username="src_ch")
    source_b = make_entity(3003, "备用源群", participants_count=88)
    main = make_entity(3002, "我的主频道", broadcast=True, username="main_ch")
    fake_account_client.dialogs = [source_a, source_b, main]
    fake_account_client.entities = {"src_ch": source_a, "main_ch": main, 3003: source_b}
    await chat_client.post("/api/sources/sync", headers=_headers())
    pool = (await chat_client.get("/api/sources/available", headers=_headers())).json()["items"]
    by_title = {item["title"]: item["id"] for item in pool}
    first, second = by_title["素材源频道"], by_title["备用源群"]
    await chat_client.post(
        "/api/sources",
        headers=_headers(),
        json={"chat_ids": [first, second]},
    )
    targets = (await chat_client.get("/api/targets/available", headers=_headers())).json()["items"]
    target_id = next(item["id"] for item in targets if item["title"] == "我的主频道")
    await chat_client.post(
        "/api/targets",
        headers=_headers(),
        json={"chat_ids": [target_id], "role": "content"},
    )

    created = await chat_client.post(
        "/api/routes",
        headers=_headers(),
        json={
            "name": "多源监听",
            "source_chat_ids": [first, second],
            "business_type": "A",
            "target_chat_ids": [target_id],
            "a_config": {"ad_policy": "none"},
        },
    )

    body = created.json()
    assert created.status_code == 201
    assert body["created"] == 2
    assert len(body["source_chat_ids"]) == 2

    listing = (await chat_client.get("/api/routes", headers=_headers())).json()
    assert listing["total"] == 2
    assert all(len(item["source_chat_ids"]) == 2 for item in listing["items"])

    # 取消其中一个源：对应的那一行要删掉
    route_id = listing["items"][0]["id"]
    patched = await chat_client.patch(
        f"/api/routes/{route_id}",
        headers=_headers(),
        json={"source_chat_ids": [first]},
    )
    assert patched.status_code == 200
    after = (await chat_client.get("/api/routes", headers=_headers())).json()
    assert after["total"] == 1
    assert after["items"][0]["source_chat_ids"] == [first]

    # 删除整条线路
    removed = await chat_client.delete(f"/api/routes/{route_id}", headers=_headers())
    assert removed.status_code == 200
    assert (await chat_client.get("/api/routes", headers=_headers())).json()["total"] == 0


async def test_route_b_config_keeps_exclude_group_ids(chat_client, fake_account_client) -> None:
    """线路能保存"引用了哪些排除词组"（共享词库多选）。"""
    source = make_entity(4001, "源频道", broadcast=True, username="src4001")
    main = make_entity(4002, "目标频道", broadcast=True, username="main4002")
    fake_account_client.dialogs = [source, main]
    fake_account_client.entities = {"src4001": source, "main4002": main}
    await chat_client.post("/api/sources/sync", headers=_headers())
    pool = (await chat_client.get("/api/sources/available", headers=_headers())).json()["items"]
    source_id = next(item["id"] for item in pool if item["title"] == "源频道")
    await chat_client.post("/api/sources", headers=_headers(), json={"chat_ids": [source_id]})
    targets = (await chat_client.get("/api/targets/available", headers=_headers())).json()["items"]
    target_id = next(item["id"] for item in targets if item["title"] == "目标频道")
    await chat_client.post(
        "/api/targets",
        headers=_headers(),
        json={"chat_ids": [target_id], "role": "lead"},
    )
    group = await chat_client.post(
        "/api/keyword-groups",
        headers=_headers(),
        json={"name": "噪声（排除）", "kind": "exclude"},
    )
    group_id = group.json()["id"]

    created = await chat_client.post(
        "/api/routes",
        headers=_headers(),
        json={
            "name": "带排除词组的线路",
            "source_chat_id": source_id,
            "business_type": "B",
            "target_chat_ids": [target_id],
            "b_config": {"listen_mode": "all", "exclude_group_ids": [group_id]},
        },
    )

    assert created.status_code == 201
    route_id = created.json()["id"]
    assert created.json()["b_config"]["exclude_group_ids"] == [group_id]
    detail = (await chat_client.get(f"/api/routes/{route_id}", headers=_headers())).json()
    assert detail["b_config"]["exclude_group_ids"] == [group_id]


async def test_add_second_source_to_single_route_joins_same_bundle(
    chat_client,
    fake_account_client,
) -> None:
    """单源线路再加一个源时，两行必须并入同一个 bundle。"""
    source_a = make_entity(3001, "素材源频道", broadcast=True, username="src_ch")
    source_b = make_entity(3003, "备用源群", participants_count=88)
    main = make_entity(3002, "我的主频道", broadcast=True, username="main_ch")
    fake_account_client.dialogs = [source_a, source_b, main]
    fake_account_client.entities = {"src_ch": source_a, "main_ch": main, 3003: source_b}
    await chat_client.post("/api/sources/sync", headers=_headers())
    pool = (await chat_client.get("/api/sources/available", headers=_headers())).json()["items"]
    by_title = {item["title"]: item["id"] for item in pool}
    first, second = by_title["素材源频道"], by_title["备用源群"]
    await chat_client.post(
        "/api/sources",
        headers=_headers(),
        json={"chat_ids": [first]},
    )
    targets = (await chat_client.get("/api/targets/available", headers=_headers())).json()["items"]
    target_id = next(item["id"] for item in targets if item["title"] == "我的主频道")
    await chat_client.post(
        "/api/targets",
        headers=_headers(),
        json={"chat_ids": [target_id], "role": "content"},
    )
    created = await chat_client.post(
        "/api/routes",
        headers=_headers(),
        json={
            "name": "先单源",
            "source_chat_id": first,
            "business_type": "A",
            "target_chat_ids": [target_id],
            "a_config": {"ad_policy": "none"},
        },
    )
    route_id = created.json()["id"]
    assert created.json()["source_chat_ids"] == [first]

    # 第二个群要先加入监听源（编辑器下拉里只列监听源）
    await chat_client.post("/api/sources", headers=_headers(), json={"chat_ids": [second]})
    patched = await chat_client.patch(
        f"/api/routes/{route_id}",
        headers=_headers(),
        json={"source_chat_ids": [first, second]},
    )

    assert patched.status_code == 200
    assert sorted(patched.json()["source_chat_ids"]) == sorted([first, second])
    listing = (await chat_client.get("/api/routes", headers=_headers())).json()
    assert listing["total"] == 2
    # 两行必须共用同一个 bundle，界面上才算一条线路
    bundles = {item["bundle_id"] for item in listing["items"]}
    assert len(bundles) == 1 and None not in bundles
