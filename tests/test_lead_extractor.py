"""会员信息提取与线索卡片渲染。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.core.lead_extractor import (
    contact_summary,
    extract_contacts,
    render_lead_card,
    sender_info,
)


def test_extract_contacts_finds_phone_wechat_and_username() -> None:
    text = "加我微信 abc12345，电话 13800138000，或者 @seller_ok"

    contacts = extract_contacts(text)

    assert "13800138000" in contacts.phones
    assert "abc12345" in contacts.wechats
    assert "seller_ok" in contacts.usernames


def test_extract_contacts_can_be_switched_off() -> None:
    text = "电话 13800138000，微信 abc12345"

    contacts = extract_contacts(text, capture_phone=False, capture_contact=False)

    assert contacts.phones == []
    assert contacts.wechats == []


def test_sender_info_from_telethon_like_object() -> None:
    sender = SimpleNamespace(
        id=7506007396,
        username="big9527",
        first_name="小",
        last_name="明",
        phone="+8613800138000",
        bot=False,
    )

    info = sender_info(sender)

    assert info.tg_user_id == 7506007396
    assert info.username == "big9527"
    assert info.display_name == "小明"
    assert info.phone == "+8613800138000"
    assert info.is_bot is False


def test_contact_summary_prefers_phone_then_username() -> None:
    sender = sender_info(SimpleNamespace(id=1, username="only_name", first_name="阿", bot=False))

    assert contact_summary(sender=sender, contacts=extract_contacts("")) == "@only_name"

    with_phone = contact_summary(sender=sender, contacts=extract_contacts("电话 13800138000"))
    assert "13800138000" in with_phone and "@only_name" in with_phone


def test_render_card_falls_back_when_no_phone() -> None:
    sender = sender_info(SimpleNamespace(id=99, username="only_name", first_name="阿", bot=False))
    contacts = extract_contacts("求个篮球赛推荐")

    card = render_lead_card(
        None,
        sender=sender,
        contacts=contacts,
        keyword="体育",
        text="求个篮球赛推荐",
        source_title="测试群",
        message_at=datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
    )

    assert "命中线索：体育" in card
    assert "@only_name" in card
    assert "99" in card
    assert "测试群" in card
    assert "2026-09-26 12:00" in card


def test_render_card_uses_custom_template() -> None:
    sender = sender_info(SimpleNamespace(id=1, username="u1", first_name="甲", bot=False))

    card = render_lead_card(
        "{昵称}|{关键词}|{联系方式}",
        sender=sender,
        contacts=extract_contacts(""),
        keyword="体育",
        text="正文",
        source_title="群",
    )

    assert card == "甲|体育|@u1"
