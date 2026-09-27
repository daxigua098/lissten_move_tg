"""B 线监听：命中关键词 → 落线索 → 推卡片。"""

from __future__ import annotations

from types import SimpleNamespace

from conftest import fake_message


async def _prepare_monitor_route(db, *, listen_mode: str = "keyword", capture_mode: str = "cold"):
    """建一条 B 线：搜索群 → 线索群，并配好关键词组。"""
    from app.core.telegram_client import ChatProfile
    from app.db.session import session_scope
    from app.services import chat_service, keyword_service, route_service

    async with session_scope() as session:
        source = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=9001,
                chat_type="supergroup",
                title="搜索群",
                username=None,
                is_private=True,
            ),
        )
        await chat_service.set_source(session, source)
        target = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=9002,
                chat_type="supergroup",
                title="线索群",
                username=None,
                is_private=True,
            ),
        )
        await chat_service.set_target(session, target, role="lead")
        group = await keyword_service.create_group(session, name="体育赛事")
        await keyword_service.add_keyword(
            session,
            group.id,
            word="体育",
            aliases="篮球,足球",
        )
        route = await route_service.create_route(
            session,
            name="搜索监听",
            source_chat_id=source.id,
            business_type="B",
            target_chat_ids=[target.id],
            b_config={
                "listen_mode": listen_mode,
                "capture_mode": capture_mode,
                "keyword_group_ids": [group.id],
            },
        )
        return route.id, source.id, target.id


def _event(
    text: str,
    *,
    message_id: int = 901,
    user_id: int = 5550001,
    username: str | None = "seller01",
    phone: str | None = None,
    access_hash: int | None = None,
    title: str | None = None,
):
    message = fake_message(message_id, text)
    sender = SimpleNamespace(
        id=user_id,
        username=username,
        first_name="卖",
        last_name="家",
        phone=phone,
        bot=False,
        access_hash=access_hash,
    )
    if title is not None:
        sender.title = title
    message.sender = sender
    return SimpleNamespace(message=message)


async def test_keyword_hit_records_lead_and_pushes_card(db, fake_delivery_client) -> None:
    from app.db.session import session_scope
    from app.services import lead_service, route_service
    from app.services.runtime_service import RuntimeService

    route_id, _source_id, _target_id = await _prepare_monitor_route(db)
    service = RuntimeService(db)

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
    await service._on_monitor_message(
        fake_delivery_client,
        _event("求个篮球赛推荐，电话 13800138000"),
        [route],
    )

    async with session_scope() as session:
        rows, total = await lead_service.list_leads(session)
    assert total == 1
    lead = rows[0]
    assert lead.keyword == "体育"
    assert lead.phone == "13800138000"
    assert lead.delivered is True
    assert lead.outreach_status == "WAITING_SENDER_ACCOUNT"
    assert lead.reachable_routes

    assert len(fake_delivery_client.sent) == 1
    card = fake_delivery_client.sent[0]["text"]
    assert "命中线索：体育" in card
    assert "13800138000" in card
    assert "@seller01" in card


async def test_full_listen_mode_scans_without_storing_unmatched_user(
    db, fake_delivery_client
) -> None:
    """全量监听仍扫描消息，但关键词未命中的用户不入线索库。"""
    from app.db.session import session_scope
    from app.services import lead_service, route_service
    from app.services.runtime_service import RuntimeService

    route_id, _source_id, _target_id = await _prepare_monitor_route(db, listen_mode="all")
    service = RuntimeService(db)

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
    await service._on_monitor_message(fake_delivery_client, _event("今天天气不错啊"), [route])

    async with session_scope() as session:
        _rows, total = await lead_service.list_leads(session)
    assert total == 0
    assert fake_delivery_client.sent == []


async def test_keyword_mode_skips_message_without_hit(db, fake_delivery_client) -> None:
    from app.db.session import session_scope
    from app.services import lead_service, route_service
    from app.services.runtime_service import RuntimeService

    route_id, _source_id, _target_id = await _prepare_monitor_route(db)
    service = RuntimeService(db)

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
    await service._on_monitor_message(fake_delivery_client, _event("今天天气不错啊"), [route])

    async with session_scope() as session:
        _rows, total = await lead_service.list_leads(session)
    assert total == 0
    assert fake_delivery_client.sent == []


async def test_cooldown_dedupes_same_person_and_keyword(db, fake_delivery_client) -> None:
    from app.db.session import session_scope
    from app.services import lead_service, route_service
    from app.services.runtime_service import RuntimeService

    route_id, _source_id, _target_id = await _prepare_monitor_route(db)
    service = RuntimeService(db)

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
    await service._on_monitor_message(
        fake_delivery_client, _event("求个篮球赛推荐", message_id=1), [route]
    )
    await service._on_monitor_message(
        fake_delivery_client, _event("再说一次篮球比赛", message_id=2), [route]
    )

    async with session_scope() as session:
        _rows, total = await lead_service.list_leads(session)
    assert total == 1
    assert len(fake_delivery_client.sent) == 1


async def test_exclude_group_blocks_message(db, fake_delivery_client) -> None:
    """选了排除词组：组里任意一个词出现，整条消息直接忽略。"""
    from app.db.session import session_scope
    from app.services import keyword_service, lead_service, route_service
    from app.services.runtime_service import RuntimeService

    route_id, source_id, _target_id = await _prepare_monitor_route(db, listen_mode="all")
    async with session_scope() as session:
        group = await keyword_service.create_group(
            session,
            name="噪声（排除）",
            kind="exclude",
        )
        await keyword_service.add_keyword(session, group.id, word="客服", aliases="小助手")
        await route_service.update_route(
            session,
            route_id,
            b_config={"listen_mode": "all", "exclude_group_ids": [group.id]},
        )
        route = await route_service.get_route(session, route_id)
        assert await keyword_service.load_exclude_words(session, [group.id]) == ["客服", "小助手"]

    service = RuntimeService(db)
    await service._on_monitor_message(
        fake_delivery_client,
        _event("我是客服 有需要找我"),
        [route],
    )
    await service._on_monitor_message(fake_delivery_client, _event("随便聊聊天气"), [route])

    async with session_scope() as session:
        _rows, total = await lead_service.list_leads(session)
    # 客服那条被排除词挡掉；普通发言没命中关键词，也不入库
    assert total == 0
    assert source_id


async def test_keyword_hit_without_reachable_route_is_not_stored(db, fake_delivery_client) -> None:
    from app.db.session import session_scope
    from app.services import lead_service, route_service
    from app.services.runtime_service import RuntimeService

    route_id, _source_id, _target_id = await _prepare_monitor_route(db)
    service = RuntimeService(db)

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
    await service._on_monitor_message(
        fake_delivery_client,
        _event("求个篮球赛推荐", username=None),
        [route],
    )

    async with session_scope() as session:
        _rows, total = await lead_service.list_leads(session)
    assert total == 0
    assert fake_delivery_client.sent == []


async def test_channel_sender_is_not_treated_as_member(db, fake_delivery_client) -> None:
    from app.db.session import session_scope
    from app.services import lead_service, route_service
    from app.services.runtime_service import RuntimeService

    route_id, _source_id, _target_id = await _prepare_monitor_route(db)
    service = RuntimeService(db)

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
    await service._on_monitor_message(
        fake_delivery_client,
        _event("求个篮球赛推荐", title="频道身份"),
        [route],
    )

    async with session_scope() as session:
        _rows, total = await lead_service.list_leads(session)
    assert total == 0
    assert fake_delivery_client.sent == []


async def test_strict_capture_requires_explicit_invite(db, fake_delivery_client) -> None:
    from app.db.session import session_scope
    from app.services import lead_service, route_service
    from app.services.runtime_service import RuntimeService

    route_id, _source_id, _target_id = await _prepare_monitor_route(db, capture_mode="strict")
    service = RuntimeService(db)

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
    await service._on_monitor_message(
        fake_delivery_client,
        _event("求个篮球赛推荐", message_id=1),
        [route],
    )
    await service._on_monitor_message(
        fake_delivery_client,
        _event("求个篮球赛推荐，有意私信我", message_id=2),
        [route],
    )

    async with session_scope() as session:
        rows, total = await lead_service.list_leads(session)
    assert total == 1
    assert rows[0].consent_type == "EXPLICIT_DM_INVITE"
    assert len(fake_delivery_client.sent) == 1


async def test_global_suppression_blocks_capture(db, fake_delivery_client) -> None:
    from app.db.models import ContactSuppression
    from app.db.session import session_scope
    from app.services import lead_service, route_service
    from app.services.runtime_service import RuntimeService

    route_id, _source_id, _target_id = await _prepare_monitor_route(db)
    service = RuntimeService(db)

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
        session.add(
            ContactSuppression(
                tenant_id=route.tenant_id,
                tg_user_id=5550001,
                reason="manual",
            )
        )
        await session.commit()
    await service._on_monitor_message(
        fake_delivery_client,
        _event("求个篮球赛推荐"),
        [route],
    )

    async with session_scope() as session:
        _rows, total = await lead_service.list_leads(session)
    assert total == 0
    assert fake_delivery_client.sent == []


async def test_existing_conversation_owner_blocks_capture(db, fake_delivery_client) -> None:
    from app.db.models import MemberProfile, TgAccount
    from app.db.session import session_scope
    from app.services import lead_service, route_service
    from app.services.runtime_service import RuntimeService

    route_id, _source_id, _target_id = await _prepare_monitor_route(db)
    service = RuntimeService(db)

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
        account = TgAccount(
            tenant_id=route.tenant_id,
            name="发送账号",
            phone_masked="+86****8000",
            phone_enc="enc",
            api_id_enc="enc",
            api_hash_enc="enc",
            session_name="sender",
        )
        session.add(account)
        await session.flush()
        session.add(
            MemberProfile(
                tenant_id=route.tenant_id,
                tg_user_id=5550001,
                conversation_owner_account_id=account.id,
            )
        )
        await session.commit()
    await service._on_monitor_message(
        fake_delivery_client,
        _event("求个篮球赛推荐"),
        [route],
    )

    async with session_scope() as session:
        _rows, total = await lead_service.list_leads(session)
    assert total == 0
    assert fake_delivery_client.sent == []


class FakeBotApi:
    """Bot API 替身：只记录发出去的卡片。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send_message(self, chat_id, text, buttons=None, disable_preview=True):  # noqa: ANN001
        self.sent.append({"chat_id": chat_id, "text": text})
        return {"message_id": 9001 + len(self.sent)}

    async def close(self) -> None:
        self.closed = True


async def test_lead_card_can_be_sent_by_bot(db, fake_delivery_client) -> None:
    """线路选了「用机器人发送」：卡片由机器人发出，不经过账号。"""
    from app.core.security import FieldCipher
    from app.db.models import ControlBot
    from app.db.session import session_scope
    from app.services import route_service
    from app.services.runtime_service import RuntimeService

    route_id, _source_id, _target_id = await _prepare_monitor_route(db)
    async with session_scope() as session:
        cipher = FieldCipher.from_config(db)
        bot = ControlBot(
            name="发送机器人",
            token_enc=cipher.encrypt("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
            enabled=True,
        )
        session.add(bot)
        await session.commit()
        await session.refresh(bot)
        await route_service.update_route(
            session,
            route_id,
            sender_mode="bot",
            notify_bot_id=bot.id,
        )
        route = await route_service.get_route(session, route_id)
        assert route.sender_mode == "bot"

    bot_api = FakeBotApi()

    async def factory(_config, _token):  # noqa: ANN001
        return bot_api

    service = RuntimeService(db, bot_api_factory=factory)
    await service._on_monitor_message(
        fake_delivery_client,
        _event("求个篮球赛推荐"),
        [route],
    )

    assert fake_delivery_client.sent == []  # 账号没有发言
    assert len(bot_api.sent) == 1  # 卡片由机器人发出
    assert "命中线索：体育" in bot_api.sent[0]["text"]
    assert bot_api.sent[0]["chat_id"].startswith("-")  # Bot API 用负数 chat_id

    await service._close_bot_apis()
    assert bot_api.closed is True
