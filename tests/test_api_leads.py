"""线索池接口测试：列表、统计与 CSV 导出。"""

from __future__ import annotations

from conftest import ADMIN_API_TOKEN, auth_header
from sqlalchemy import select


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


async def test_leads_survive_route_deletion(admin_client, api_config) -> None:
    """线索是业务数据：删线路不能把它一起删掉（只解除引用）。"""
    await _seed_lead(api_config)
    routes = (await admin_client.get("/api/routes", headers=_headers())).json()["items"]
    route_id = routes[0]["id"]

    removed = await admin_client.delete(f"/api/routes/{route_id}", headers=_headers())
    assert removed.status_code == 200

    listing = await admin_client.get("/api/leads", headers=_headers())
    body = listing.json()
    assert body["total"] == 1
    assert body["items"][0]["route_id"] is None
    assert body["items"][0]["phone"] == "13800138000"


async def test_purge_keeps_hits_forever_and_drops_others(db) -> None:
    """保留策略分两档：命中关键词的线索与用户永久保留，未命中的才清理。"""
    from datetime import timedelta

    from app.core.lead_extractor import ContactInfo, SenderInfo
    from app.core.telegram_client import ChatProfile
    from app.db.base import utc_now
    from app.db.models import MemberProfile
    from app.db.session import session_scope
    from app.services import chat_service, lead_service, route_service

    old = utc_now() - timedelta(days=30)
    async with session_scope() as session:
        source = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=8901,
                chat_type="supergroup",
                title="来源群",
                username=None,
                is_private=True,
            ),
        )
        await chat_service.set_source(session, source)
        target = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=8902,
                chat_type="supergroup",
                title="线索群",
                username=None,
                is_private=True,
            ),
        )
        await chat_service.set_target(session, target, role="lead")
        route = await route_service.create_route(
            session,
            name="保留策略测试",
            source_chat_id=source.id,
            business_type="B",
            target_chat_ids=[target.id],
            b_config={"listen_mode": "all"},
        )
        hit_sender = SenderInfo(tg_user_id=7001, username="hit_user", display_name="命中的")
        plain_sender = SenderInfo(tg_user_id=7002, username="plain_user", display_name="没命中的")
        # 命中线索（30 天前）与未命中线索（30 天前）
        hit_lead = await lead_service.record_lead(
            session,
            route_id=route.id,
            source_chat_id=source.id,
            message_id=601,
            message_at=old,
            sender=hit_sender,
            contacts=ContactInfo(phones=["13800138000"]),
            keyword="体育",
            keyword_group_id=None,
            matched_mode="contains",
            score=1.0,
            text="求个篮球赛 13800138000",
            source_title="来源群",
        )
        hit_lead.created_at = old
        plain_lead = await lead_service.record_lead(
            session,
            route_id=route.id,
            source_chat_id=source.id,
            message_id=602,
            message_at=old,
            sender=plain_sender,
            contacts=ContactInfo(),
            keyword=None,
            keyword_group_id=None,
            matched_mode="",
            score=0.0,
            text="随便聊聊",
            source_title="来源群",
        )
        plain_lead.created_at = old
        await lead_service.upsert_member(session, hit_sender, seen_at=old, hit=True)
        await lead_service.upsert_member(session, plain_sender, seen_at=old, hit=False)
        profiles = list(await session.scalars(select(MemberProfile)))
        for profile in profiles:
            profile.last_seen_at = old
        await session.commit()

    async with session_scope() as session:
        result = await lead_service.purge_expired(
            session,
            leads_days=3,
            profiles_days=3,
            archive_dir=db.path("data/archive"),
        )
        rows, total = await lead_service.list_leads(session)
        stats = await lead_service.lead_stats(session)

    assert result["leads"] == 1  # 只清理未命中的那条
    assert result["archived"] == 1
    assert result["profiles"] == 1  # 只清理没命中过的用户
    assert total == 1
    assert rows[0].keyword == "体育"
    assert stats["hits"] == 1
    assert stats["pinned_members"] == 1
    # 命中的线索没被归档（永久保留）
    archive_files = list(db.path("data/archive").glob("*.jsonl"))
    assert len(archive_files) == 1
    archived = archive_files[0].read_text(encoding="utf-8")
    assert "随便聊聊" in archived
    assert "求个篮球赛" not in archived
