"""本地演练端到端流程：同步群组 → 建源/接收组 → 建线路 → 补齐历史 → 投递 → 挂广告。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from conftest import ADMIN_API_TOKEN, auth_header, database_url
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def demo_config(project_root, valid_secret_key):
    """开启演示模式的配置。"""
    from app.core.config import load_config

    return load_config(
        project_root=project_root,
        environ={
            "SECRET_KEY": valid_secret_key,
            "ADMIN_PASSWORD": "custom-pass1",
            "ADMIN_API_TOKEN": ADMIN_API_TOKEN,
            "DATABASE_URL": database_url(project_root),
            "DEMO_MODE": "true",
        },
    )


@pytest.fixture
async def demo_client(demo_config) -> AsyncIterator[AsyncClient]:
    """演示模式下的接口客户端（用模拟客户端代替 Telegram）。"""
    from app.api.app import create_app
    from app.db.session import create_schema, dispose_database, init_database

    assert demo_config.app.demo_mode is True
    await init_database(demo_config)
    await create_schema()
    app = create_app(demo_config)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        await dispose_database()


async def _bootstrap(demo_client) -> dict:
    """注册演示账号 → 同步群组 → 建源与接收组 → 建 A 线与广告素材。"""
    headers = auth_header(ADMIN_API_TOKEN)

    account = await demo_client.post(
        "/api/accounts",
        headers=headers,
        json={
            "name": "演示主号",
            "phone": "+8613800001111",
            "api_id": 123456,
            "api_hash": "demo-api-hash-0123456789abcdef",
            "is_default": True,
        },
    )
    assert account.status_code == 201

    sync = await demo_client.post("/api/sources/sync", headers=headers)
    assert sync.status_code == 200
    assert sync.json()["fetched"] == 4

    pool = (await demo_client.get("/api/sources/available", headers=headers)).json()["items"]
    source_id = next(item["id"] for item in pool if item["title"] == "演示素材频道")
    await demo_client.post("/api/sources", headers=headers, json={"chat_ids": [source_id]})

    target_pool = (await demo_client.get("/api/targets/available", headers=headers)).json()["items"]
    main_id = next(item["id"] for item in target_pool if item["title"] == "演示福利频道")
    added = await demo_client.post(
        "/api/targets",
        headers=headers,
        json={"chat_ids": [main_id], "role": "content"},
    )
    assert added.status_code == 201

    asset = await demo_client.post(
        "/api/ad-assets",
        headers=headers,
        json={
            "name": "演示广告",
            "text": "{源名} · 每日更新",
            "link_url": "https://t.me/demo?start=a",
            "link_text": "立即查看",
        },
    )
    assert asset.status_code == 201

    route = await demo_client.post(
        "/api/routes",
        headers=headers,
        json={
            "name": "演示线路",
            "source_chat_id": source_id,
            "business_type": "A",
            "target_chat_ids": [main_id],
            "a_config": {
                "content_types": ["text", "photo", "video"],
                "ad_policy": "every",
                "ad_asset_id": asset.json()["id"],
                "promo_blacklist": ["加我微信"],
            },
        },
    )
    assert route.status_code == 201, route.text
    return {
        "headers": headers,
        "route_id": route.json()["id"],
        "asset_id": asset.json()["id"],
    }


async def test_demo_pipeline_end_to_end(demo_client, demo_config) -> None:
    ctx = await _bootstrap(demo_client)

    from app.core.demo_client import DemoAccountClient
    from app.core.route_config import load_a_config
    from app.db.models import AdAsset, Chat
    from app.db.session import session_scope
    from app.services import delivery_service, history_service, route_service

    client = DemoAccountClient(log_actions=False)
    source_entity = await client.get_entity(910001)

    # 1) 补齐历史并入队
    async with session_scope() as session:
        route = await route_service.get_route(session, ctx["route_id"])
        result = await history_service.sync_route_history(
            session,
            demo_config,
            route=route,
            client=client,
            a_config=load_a_config(route.a_config),
            source_entity=source_entity,
        )

    assert result["targets"] == 1
    # 演示源 12 条：置顶 1 条在拉取阶段就被跳过，其余 11 条全部入队
    assert result["enqueued"] == 11
    assert result["inspected"] == 11
    assert result["filtered"] == 0

    # 2) 串行投递（模拟运行时队列循环）
    delivered = 0
    async with session_scope() as session:
        route = await route_service.get_route(session, ctx["route_id"])
        a_config = load_a_config(route.a_config)
        ad_asset = await session.get(AdAsset, ctx["asset_id"])
        target_entity = await client.get_entity(910002)
        while True:
            job = await delivery_service.next_ready_job(session)
            if job is None:
                break
            source_chat = await session.get(Chat, job.source_chat_id)
            target_chat = await session.get(Chat, job.target_chat_id)
            await delivery_service.deliver_job(
                session,
                demo_config,
                job=job,
                route=route,
                client=client,
                source_chat=source_chat,
                target_chat=target_chat,
                a_config=a_config,
                ad_asset=ad_asset,
                source_entity=source_entity,
                target_entity=target_entity,
            )
            delivered += 1

    assert delivered == 11

    # 3) 校验动作：11 次转发（去来源标记）+ 每条都挂广告
    forwards = [item for item in client.actions if item["action"] == "forward"]
    ads = [item for item in client.actions if item["action"].startswith("ad_")]
    assert len(forwards) == 11
    assert all(item["drop_author"] is True for item in forwards)
    assert len(ads) == 11
    assert ads[0]["text"].startswith("演示素材频道")
    assert "{源名}" not in ads[0]["text"]

    # 4) 接口可见：全部成功、统计正确、水位线推进
    jobs = await demo_client.get("/api/jobs", headers=ctx["headers"])
    assert jobs.json()["total"] == 11
    assert {item["status"] for item in jobs.json()["items"]} == {"success"}
    assert all(item["ad_applied"] for item in jobs.json()["items"])

    stats = await demo_client.get("/api/jobs/stats", headers=ctx["headers"])
    success = next(item for item in stats.json()["items"] if item["status"] == "success")
    assert success["count"] == 11

    progress = await demo_client.get(
        f"/api/routes/{ctx['route_id']}/progress",
        headers=ctx["headers"],
    )
    assert progress.json()["items"][0]["last_delivered_message_id"] == 12

    # 5) 再补一次历史：水位线生效，零重复
    async with session_scope() as session:
        route = await route_service.get_route(session, ctx["route_id"])
        second = await history_service.sync_route_history(
            session,
            demo_config,
            route=route,
            client=client,
            a_config=load_a_config(route.a_config),
            source_entity=source_entity,
        )
    assert second["enqueued"] == 0


async def test_demo_runtime_status(demo_client) -> None:
    response = await demo_client.get("/api/runtime/status", headers=auth_header(ADMIN_API_TOKEN))

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "stopped"
    assert body["paused"] is False
    assert "jobs" in body
