"""链接滚雪球用的解析逻辑（F-R03 / F-R05）。"""

from __future__ import annotations

from app.core.link_extractor import extract_link_keys, extract_links


def test_extracts_username_and_invite_forms() -> None:
    text = (
        "找群看 https://t.me/GoodGroups 和 t.me/+AbCdEf123456 ，"
        "另外 telegram.me/other_group 也行，直接 @Third_Group"
    )
    hits = extract_links(text)

    assert [(item.kind, item.value) for item in hits] == [
        ("username", "GoodGroups"),
        ("invite", "AbCdEf123456"),
        ("username", "other_group"),
        ("username", "Third_Group"),
    ]


def test_message_link_uses_first_segment() -> None:
    """t.me/xxx/123 是消息链接，但它确实指向 xxx 这个频道。"""
    hits = extract_links("原帖：https://t.me/daily_news/12345")

    assert len(hits) == 1
    assert hits[0].kind == "username"
    assert hits[0].value == "daily_news"


def test_joinchat_and_plus_are_the_same_invite() -> None:
    hits = extract_links("t.me/joinchat/AbCdEf123456 与 t.me/+AbCdEf123456 是同一个群")

    assert len(hits) == 1
    assert hits[0].kind == "invite"
    assert hits[0].value == "AbCdEf123456"
    assert hits[0].normalized_key == "invite:AbCdEf123456"


def test_reserved_paths_are_ignored() -> None:
    text = (
        "预览 t.me/s/some_channel ；内部链接 t.me/c/123456/78 ；"
        "贴纸 t.me/addstickers/pack ；分享 t.me/share/url"
    )

    assert extract_links(text) == []


def test_duplicate_forms_collapse_to_one_key() -> None:
    """同一条消息里既贴链接又写 @，指向同一个群只留一条。"""
    hits = extract_links("官方频道 @SomeChannel ，链接 https://t.me/somechannel")

    assert len(hits) == 1
    # 先扫 URL 再扫 @，所以留下的是「链接里的写法」
    assert hits[0].value == "somechannel"


def test_extract_link_keys_is_order_free() -> None:
    keys = extract_link_keys("@AAAA and t.me/+bbbBBBbbbBBB")

    assert keys == {"username:aaaa", "invite:bbbBBBbbbBBB"}


def test_plain_text_has_no_links() -> None:
    assert extract_links("今天天气不错，有人聊天吗") == []
    assert extract_link_keys(None) == set()
