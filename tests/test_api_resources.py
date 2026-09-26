"""资源发现接口：列表、采集、刷新、加群与采纳（P-R01 / P-R03）。"""

from __future__ import annotations

from tests.conftest import ADMIN_USERNAME, auth_header, login, resource_message


async def _token(client) -> dict[str, str]:
    response = await login(client)
    assert response.status_code == 200, response.text
    return auth_header(response.json()["token"])


async def _seed_resource(fake_resource_client, *, tg_id: int = 4001, **kwargs) -> dict:
    fake_resource_client.add_chat(
        tg_id,
        kwargs.get("title", "求职大群"),
        username=kwargs.get("username", "job_big"),
        about=kwargs.get("about", "每天发布招聘信息"),
        member_count=kwargs.get("member_count", 8000),
    )
    fake_resource_client.add_history(
        tg_id,
        kwargs.get("messages")
        or [resource_message(i, "招聘日结，当天结算", sender_id=100 + i) for i in range(4)],
    )
    return {"tg_id": tg_id}


async def test_list_is_empty_before_collecting(resource_api_client) -> None:
    headers = await _token(resource_api_client)

    response = await resource_api_client.get("/api/resources", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 100, "offset": 0}


async def test_overview_and_facets(resource_api_client) -> None:
    headers = await _token(resource_api_client)

    overview = await resource_api_client.get("/api/resources/overview", headers=headers)
    facets = await resource_api_client.get("/api/resources/facets", headers=headers)

    assert overview.status_code == 200
    body = overview.json()
    assert body["searches_left"] == body["search_daily_limit"]
    assert body["join_daily_limit"] == 50
    assert facets.json()["sorts"][0]["value"] == "activity"


async def test_import_resolves_and_probes(resource_api_client, fake_resource_client) -> None:
    headers = await _token(resource_api_client)
    await _seed_resource(fake_resource_client)

    response = await resource_api_client.post(
        "/api/resources/import",
        headers=headers,
        json={"inputs": ["https://t.me/job_big"], "join": False},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["failures"] == []
    assert body["added"][0]["title"] == "求职大群"
    assert body["added"][0]["probed"] is True

    listing = await resource_api_client.get("/api/resources", headers=headers)
    items = listing.json()["items"]
    assert len(items) == 1
    assert items[0]["member_count"] == 8000
    assert items[0]["activity_score"] > 0
    assert items[0]["freshness"] == "fresh"
    assert items[0]["status"] == "probed"


async def test_import_reports_per_item_failure(resource_api_client) -> None:
    headers = await _token(resource_api_client)

    response = await resource_api_client.post(
        "/api/resources/import",
        headers=headers,
        json={"inputs": ["这不是链接", "+8613800001111"]},
    )

    assert response.status_code == 201
    failures = response.json()["failures"]
    assert len(failures) == 2
    assert "无法识别" in failures[0]["reason"]
    assert "手机号" in failures[1]["reason"]


async def test_import_requires_join_for_invite(resource_api_client) -> None:
    headers = await _token(resource_api_client)

    response = await resource_api_client.post(
        "/api/resources/import",
        headers=headers,
        json={"inputs": ["t.me/+AbCdEf123456"]},
    )

    assert response.status_code == 201
    failures = response.json()["failures"]
    assert "允许加入" in failures[0]["reason"]


async def test_detail_includes_samples_and_history(
    resource_api_client,
    fake_resource_client,
) -> None:
    headers = await _token(resource_api_client)
    await _seed_resource(fake_resource_client)
    await resource_api_client.post(
        "/api/resources/import",
        headers=headers,
        json={"inputs": ["https://t.me/job_big"]},
    )
    resource_id = (await resource_api_client.get("/api/resources", headers=headers)).json()[
        "items"
    ][0]["id"]

    response = await resource_api_client.get(
        f"/api/resources/{resource_id}",
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["samples"]) == 4
    assert body["samples"][0]["text"].startswith("招聘日结")
    assert body["samples"][0]["is_bot"] is False
    assert len(body["history"]) == 1
    assert body["history"][0]["result"] == "ok"


async def test_manual_fields_are_locked(resource_api_client, fake_resource_client) -> None:
    headers = await _token(resource_api_client)
    await _seed_resource(fake_resource_client)
    await resource_api_client.post(
        "/api/resources/import",
        headers=headers,
        json={"inputs": ["https://t.me/job_big"]},
    )
    resource_id = (await resource_api_client.get("/api/resources", headers=headers)).json()[
        "items"
    ][0]["id"]

    patched = await resource_api_client.patch(
        f"/api/resources/{resource_id}",
        headers=headers,
        json={"categories": ["我自己标的"], "is_favorite": True, "note": "重点跟"},
    )
    assert patched.status_code == 200
    assert patched.json()["categories"] == ["我自己标的"]
    assert patched.json()["is_favorite"] is True
    assert "categories" in patched.json()["manual_locked"]

    refreshed = await resource_api_client.post(
        f"/api/resources/{resource_id}/refresh",
        headers=headers,
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["resource"]["categories"] == ["我自己标的"]


async def test_blacklist_filters_and_blocks_discovery(
    resource_api_client,
    fake_resource_client,
) -> None:
    headers = await _token(resource_api_client)
    await _seed_resource(fake_resource_client)
    await resource_api_client.post(
        "/api/resources/import",
        headers=headers,
        json={"inputs": ["https://t.me/job_big"]},
    )
    resource_id = (await resource_api_client.get("/api/resources", headers=headers)).json()[
        "items"
    ][0]["id"]

    await resource_api_client.patch(
        f"/api/resources/{resource_id}",
        headers=headers,
        json={"is_blacklisted": True, "blacklist_reason": "广告太多"},
    )

    blacklisted = await resource_api_client.get(
        "/api/resources",
        headers=headers,
        params={"blacklisted": "true"},
    )
    assert blacklisted.json()["total"] == 1

    # 再补搜一次同一个群：黑名单资源不会被重新收录
    fake_resource_client.add_search("求职", [fake_resource_client.entities[4001]])
    collected = await resource_api_client.post(
        "/api/resources/discover-online",
        headers=headers,
        json={"keywords": ["求职"], "sites": ["telegram"]},
    )
    assert collected.status_code == 200
    assert collected.json()["new_total"] == 0


async def test_join_queue_enqueue(resource_api_client, fake_resource_client) -> None:
    headers = await _token(resource_api_client)
    await _seed_resource(fake_resource_client, tg_id=4201, username="join_me")
    await resource_api_client.post(
        "/api/resources/import",
        headers=headers,
        json={"inputs": ["https://t.me/join_me"]},
    )
    resource_id = (await resource_api_client.get("/api/resources", headers=headers)).json()[
        "items"
    ][0]["id"]

    response = await resource_api_client.post(
        "/api/resources/join",
        headers=headers,
        json={"ids": [resource_id]},
    )

    assert response.status_code == 201
    assert response.json()["failures"] == []
    assert response.json()["queued"][0]["status"] == "pending"
    assert response.json()["queued"][0]["resource_title"] == "求职大群"


async def test_adopt_creates_source_and_route(resource_api_client, fake_resource_client) -> None:
    headers = await _token(resource_api_client)
    await _seed_resource(fake_resource_client, tg_id=4301, username="adopt_me")
    await resource_api_client.post(
        "/api/resources/import",
        headers=headers,
        json={"inputs": ["https://t.me/adopt_me"]},
    )
    resource_id = (await resource_api_client.get("/api/resources", headers=headers)).json()[
        "items"
    ][0]["id"]

    from app.db.session import session_scope
    from app.services import chat_service

    async with session_scope() as session:
        profile = fake_resource_client.add_chat(
            4302,
            "接收群",
            username="target_group",
            megagroup=True,
        )
        from app.core.telegram_client import profile_from_entity

        chat = await chat_service.upsert_chat_from_profile(
            session,
            profile_from_entity(profile),
            joined=True,
        )
        await chat_service.set_target(session, chat, role="lead")
        target_chat_id = chat.id

    response = await resource_api_client.post(
        f"/api/resources/{resource_id}/adopt",
        headers=headers,
        json={
            "create_route": True,
            "business_type": "B",
            "target_chat_ids": [target_chat_id],
            "route_name": "资源群 → 接收群",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resource"]["status"] == "adopted"
    assert body["resource"]["adopted_by"] == ADMIN_USERNAME
    assert len(body["routes"]) == 1
    # 这条资源已经探测成功（说明账号在群里），不用再排队加群
    assert body["join_task"] is None

    sources = await resource_api_client.get("/api/sources", headers=headers)
    assert any(item["tg_id"] == 4301 for item in sources.json()["items"])

    routes = await resource_api_client.get("/api/routes", headers=headers)
    assert routes.json()["total"] == 1
    assert routes.json()["items"][0]["name"] == "资源群 → 接收群"


async def test_adopt_unprobed_resource_queues_join(
    resource_api_client,
    fake_resource_client,
) -> None:
    """没探测过 = 还不知道在不在群里，采纳时顺手排一次加群。"""
    headers = await _token(resource_api_client)
    await _seed_resource(fake_resource_client, tg_id=4311, username="pending_join")
    await resource_api_client.post(
        "/api/resources/import",
        headers=headers,
        json={"inputs": ["https://t.me/pending_join"], "probe": False},
    )
    resource_id = (await resource_api_client.get("/api/resources", headers=headers)).json()[
        "items"
    ][0]["id"]

    response = await resource_api_client.post(
        f"/api/resources/{resource_id}/adopt",
        headers=headers,
        json={},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resource"]["status"] == "adopted"
    assert body["join_task"] is not None
    assert body["join_task"]["status"] == "pending"


async def test_resource_exposes_public_link_and_join_state(
    resource_api_client,
    fake_resource_client,
) -> None:
    """卡片要能点开公开链接（Telegram 会提示加入），并看见账号加群状态。"""
    headers = await _token(resource_api_client)
    await _seed_resource(fake_resource_client, tg_id=4401, username="public_group")
    await resource_api_client.post(
        "/api/resources/import",
        headers=headers,
        json={"inputs": ["https://t.me/public_group"]},
    )

    item = (await resource_api_client.get("/api/resources", headers=headers)).json()["items"][0]
    assert item["link"] == "https://t.me/public_group"
    # 还没排过加群，状态为空
    assert item["join"] is None

    queued = await resource_api_client.post(
        "/api/resources/join",
        headers=headers,
        json={"ids": [item["id"]]},
    )
    assert queued.status_code == 201
    assert queued.json()["failures"] == []

    listed = (await resource_api_client.get("/api/resources", headers=headers)).json()["items"][0]
    assert listed["join"]["status"] == "pending"
    assert listed["join"]["scheduled_at"] is not None

    detail = await resource_api_client.get(f"/api/resources/{item['id']}", headers=headers)
    assert detail.json()["join"]["status"] == "pending"
    assert detail.json()["link"] == "https://t.me/public_group"


async def test_overview_reports_runtime_state(resource_api_client) -> None:
    """概览要说明运行时到底在不在跑——它停了加群与探测都不会执行。"""
    headers = await _token(resource_api_client)

    response = await resource_api_client.get("/api/resources/overview", headers=headers)

    assert response.status_code == 200
    runtime = response.json()["runtime"]
    assert runtime["running"] is False
    assert runtime["status"] == "stopped"


async def test_export_csv(resource_api_client, fake_resource_client) -> None:
    headers = await _token(resource_api_client)
    await _seed_resource(fake_resource_client, tg_id=4401, username="csv_group")
    await resource_api_client.post(
        "/api/resources/import",
        headers=headers,
        json={"inputs": ["https://t.me/csv_group"]},
    )

    response = await resource_api_client.get("/api/resources/export.csv", headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    text = response.content.decode("utf-8")
    assert text.startswith("\ufeff")
    assert "真人活跃度" in text.splitlines()[0]
    assert "求职大群" in text


async def test_detail_404_for_unknown_resource(resource_api_client) -> None:
    headers = await _token(resource_api_client)

    response = await resource_api_client.get("/api/resources/999999", headers=headers)

    assert response.status_code == 404


async def test_viewer_cannot_read_resources(resource_api_client) -> None:
    """只读角色不该看到资源库（与现有模块的权限口径一致）。"""
    admin_headers = await _token(resource_api_client)
    created = await resource_api_client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "username": "viewer1",
            "password": "viewer-pass1",
            "role": "viewer",
            "display_name": "只读",
        },
    )
    assert created.status_code in (200, 201), created.text

    token = await login(resource_api_client, "viewer1", "viewer-pass1")
    headers = auth_header(token.json()["token"])

    response = await resource_api_client.get("/api/resources", headers=headers)

    assert response.status_code == 403
