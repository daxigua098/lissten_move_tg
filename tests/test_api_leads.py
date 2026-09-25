"""线索池接口测试：列表、统计与 CSV 导出。"""

from __future__ import annotations

from conftest import ADMIN_API_TOKEN, auth_header


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


async def _seed_lead(api_config) -> None:
    """造一条线索：群组 → 线路 → 线索。"""
    from app.core.lead_extractor import ContactInfo, SenderInfo
    from app.core.telegram_client import ChatProfile
    from app.db.session import session_scope
    from app.services import chat_service, lead_service, route_service

    async with session_scope() as session:
        source = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=8801,
                chat_type="supergroup",
                title="搜索资源群",
                username=None,
                is_private=True,
            ),
        )
        await chat_service.set_source(session, source)
        target = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=8802,
                chat_type="supergroup",
                title="线索群",
                username=None,
                is_private=True,
            ),
        )
        await chat_service.set_target(session, target, role="lead")
        route = await route_service.create_route(
            session,
            name="搜索监听",
            source_chat_id=source.id,
            business_type="B",
            target_chat_ids=[target.id],
            b_config={"listen_mode": "all"},
        )
        await lead_service.record_lead(
            session,
            route_id=route.id,
            source_chat_id=source.id,
            message_id=501,
            message_at=None,
            sender=SenderInfo(
                tg_user_id=6660001,
                username="seller01",
                display_name="卖家一号",
            ),
            contacts=ContactInfo(phones=["13800138000"], wechats=["abc12345"]),
            keyword="体育",
            keyword_group_id=None,
            matched_mode="contains",
            score=1.0,
            text="求个篮球赛推荐，电话 13800138000",
            source_title="搜索资源群",
        )


async def test_leads_list_stats_and_export(admin_client, api_config) -> None:
    await _seed_lead(api_config)

    listing = await admin_client.get("/api/leads", headers=_headers())
    body = listing.json()
    assert listing.status_code == 200
    assert body["total"] == 1
    item = body["items"][0]
    assert item["keyword"] == "体育"
    assert item["phone"] == "13800138000"
    assert item["sender_username"] == "seller01"
    assert item["delivered"] is False

    stats = await admin_client.get("/api/leads/stats", headers=_headers())
    assert stats.json()["total"] == 1
    assert stats.json()["today"] == 1

    export = await admin_client.get("/api/leads/export.csv", headers=_headers())
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    text = export.content.decode("utf-8")
    assert text.startswith("\ufeff")  # Excel 需要的 BOM
    assert "手机号" in text and "13800138000" in text and "体育" in text


async def test_leads_filter_by_keyword(admin_client, api_config) -> None:
    await _seed_lead(api_config)

    hit = await admin_client.get("/api/leads?keyword=体育", headers=_headers())
    miss = await admin_client.get("/api/leads?keyword=不存在的词", headers=_headers())

    assert hit.json()["total"] == 1
    assert miss.json()["total"] == 0
