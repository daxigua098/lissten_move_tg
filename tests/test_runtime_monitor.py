"""B 线监听：命中关键词 → 落线索 → 推卡片。"""

from __future__ import annotations

from types import SimpleNamespace

from conftest import fake_message


async def _prepare_monitor_route(db, *, listen_mode: str = "keyword"):
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
            b_config={"listen_mode": listen_mode, "keyword_group_ids": [group.id]},
        )
        return route.id, source.id, target.id


def _event(text: str, *, message_id: int = 901, user_id: int = 5550001):
    message = fake_message(message_id, text)
    message.sender = SimpleNamespace(
        id=user_id,
        username="seller01",
        first_name="卖",
        last_name="家",
        phone=None,
        bot=False,
    )
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

    assert len(fake_delivery_client.sent) == 1
    card = fake_delivery_client.sent[0]["text"]
    assert "命中线索：体育" in card
    assert "13800138000" in card
    assert "@seller01" in card


async def test_full_listen_mode_only_stores_without_push(db, fake_delivery_client) -> None:
    """全量监听：没命中关键词也入库，但默认不推卡片。"""
    from app.db.session import session_scope
    from app.services import lead_service, route_service
    from app.services.runtime_service import RuntimeService

    route_id, _source_id, _target_id = await _prepare_monitor_route(db, listen_mode="all")
    service = RuntimeService(db)

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
    await service._on_monitor_message(fake_delivery_client, _event("今天天气不错啊"), [route])

    async with session_scope() as session:
        rows, total = await lead_service.list_leads(session)
    assert total == 1
    assert rows[0].keyword is None
    assert rows[0].delivered is False
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
        rows, total = await lead_service.list_leads(session)
    # 客服那条被排除词挡掉，只剩普通发言
    assert total == 1
    assert rows[0].text == "随便聊聊天气"
    assert source_id


class FakeBotSender:
    """机器人客户端替身：只实现发送与预热对话列表。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.dialog_calls = 0
        self.disconnected = False

    async def get_dialogs(self, limit=None):  # noqa: ANN001
        self.dialog_calls += 1
        return []

    async def get_entity(self, identifier):  # noqa: ANN001
        return identifier

    async def send_message(self, entity, text, buttons=None):  # noqa: ANN001
        self.sent.append({"target": entity, "text": text})
        return SimpleNamespace(id=9001 + len(self.sent))

    async def disconnect(self) -> None:
        self.disconnected = True


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

    bot_sender = FakeBotSender()

    async def factory(_config, _token):  # noqa: ANN001
        return bot_sender

    service = RuntimeService(db, bot_client_factory=factory)
    await service._on_monitor_message(
        fake_delivery_client,
        _event("求个篮球赛推荐"),
        [route],
    )

    assert fake_delivery_client.sent == []  # 账号没有发言
    assert len(bot_sender.sent) == 1  # 卡片由机器人发出
    assert "命中线索：体育" in bot_sender.sent[0]["text"]
    assert bot_sender.dialog_calls == 1  # 预热过一次

    await service._close_bot_clients()
    assert bot_sender.disconnected is True
