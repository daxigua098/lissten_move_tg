"""本地演练模式：模拟客户端行为测试。"""

from __future__ import annotations

from app.core.content_cleaner import KIND_PHOTO, KIND_TEXT, CleanRules, clean_text
from app.core.demo_client import (
    DemoAccountClient,
    DemoBotClient,
    demo_account_client_factory,
    demo_bot_client_factory,
)
from app.core.source_resolver import resolve_target
from app.core.telegram_client import (
    ChatProfile,
    fetch_bot_profile,
    fetch_chat_profile,
    fetch_dialogs,
    join_invite,
    message_view_from_telethon,
)


async def test_demo_dialogs_and_entities() -> None:
    client = DemoAccountClient(log_actions=False)

    dialogs = await fetch_dialogs(client)
    assert len(dialogs) == 4
    assert {item.title for item in dialogs} >= {"演示素材频道", "演示交流群"}

    profile = await fetch_chat_profile(client, resolve_target("t.me/demo_material"))
    assert isinstance(profile, ChatProfile)
    assert profile.tg_id == 910001
    assert profile.chat_type == "channel"

    unknown = await fetch_chat_profile(client, resolve_target("@new_demo_channel"))
    assert unknown.tg_id > 0
    assert unknown.title == "new_demo_channel"


async def test_demo_invite_join() -> None:
    client = DemoAccountClient(log_actions=False)

    profile = await join_invite(client, "AbCdEfGh123456")

    assert profile.title == "演示私有群"
    assert profile.is_private is True


async def test_demo_history_messages_cover_cleanable_content() -> None:
    client = DemoAccountClient(log_actions=False)
    entity = await client.get_entity(910001)

    messages = await client.get_messages(entity, min_id=0, limit=12, reverse=True)
    views = [message_view_from_telethon(item) for item in messages]

    assert [view.message_id for view in views] == list(range(1, 13))
    assert any(view.kind == KIND_PHOTO for view in views)
    # 第 5 条带对方广告与推广词，净化后应只剩正文
    promo = next(view for view in views if view.message_id == 5)
    assert promo.kind == KIND_TEXT
    cleaned = clean_text(
        promo.text,
        CleanRules(promo_blacklist=("加我微信",)),
    )
    assert "https://ad.example.com" not in cleaned
    assert "@demo_seller" not in cleaned
    assert "加我微信" not in cleaned
    assert cleaned.startswith("演示广告帖")


async def test_demo_forward_and_send_are_recorded() -> None:
    client = DemoAccountClient(log_actions=False)
    source = await client.get_entity(910001)
    target = await client.get_entity(910002)

    first = await client.forward_messages(target, [3, 7], source, drop_author=True)
    second = await client.send_message(target, "广告文案", buttons=None)
    third = await client.send_file(target, file=None, caption="图片广告")

    assert first.id != second.id != third.id
    actions = [item["action"] for item in client.actions]
    assert actions == ["forward", "ad_text", "ad_file"]
    assert client.actions[0]["drop_author"] is True
    assert client.actions[0]["message_ids"] == [3, 7]


async def test_demo_bot_profile() -> None:
    factory = demo_bot_client_factory()
    client = await factory(None, "123456:DEMO-TOKEN-VALUE")

    profile = await fetch_bot_profile(client)

    assert profile.username == "demo_control_bot"
    assert profile.tg_user_id == 9300001
    assert isinstance(client, DemoBotClient)


async def test_demo_account_factory_signature_matches_real_one() -> None:
    factory = demo_account_client_factory()

    client = await factory(
        None,
        api_id=123456,
        api_hash="x" * 32,
        session_path="data/sessions/demo",
    )

    assert isinstance(client, DemoAccountClient)
